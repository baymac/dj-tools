#!/usr/bin/env node
/**
 * Long-running batch helper that drives DJ Studio's bundled libraries to
 * analyse Beatport tracks. Replaces the 30s preview pipeline with full-track
 * audio fetched via the SDK.
 *
 * Protocol:
 *   stdin  (one JSON object per line):
 *     {"cmd": "init", "stagingApi": false, "djsAccessJwt": "..."}    once
 *     {"cmd": "analyze", "beatport_id": 12345}                        per track
 *     {"cmd": "exit"}                                                 to finish
 *
 *   stdout (one JSON object per line):
 *     {"event": "ready"}
 *     {"event": "analysis", "beatport_id": 12345, "result": { ... }}
 *     {"event": "error", "beatport_id": 12345, "message": "..."}
 *     {"event": "log", "message": "..."}            (informational)
 *     {"event": "exit"}
 *
 * Path A constraint: DJ Studio MUST be quit before this runs, otherwise
 * the SDK can't bind localhost:61894 and `init` will hang.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const readline = require('node:readline');

// ── Bundled DJ Studio assets ──────────────────────────────────────────────────

const DJ_APP        = '/Applications/DJ.Studio.app';
const UNPACKED      = `${DJ_APP}/Contents/Resources/app.asar.unpacked`;
const SDK_NODE      = `${UNPACKED}/node_modules/@appmachine/beatport-sdk/build/Release/beatport_sdk.node`;
const STEMS_NODE    = `${UNPACKED}/node_modules/@appmachine/ai-stems/build/Release/ai-stems.node`;
const BEATGRID_NODE = `${UNPACKED}/node_modules/@appmachine/ai-beatgrid/build/Release/ai-beatgrid.node`;
const MIK_WASM      = `${DJ_APP}/Contents/Resources/public/5/key-feature-extractor.js`;

const BEATGRID_MODEL_BEATS   = `${UNPACKED}/node_modules/@appmachine/ai-beatgrid/build/Release/model_fold_0.pt`;
const BEATGRID_MODEL_PHRASES = `${UNPACKED}/node_modules/@appmachine/ai-beatgrid/build/Release/model_phrases.pt`;

const STEM_MODELS_DIR = path.join(os.homedir(), 'Library', 'Application Support', 'DJ.Studio', 'extensions', 'djs-stems', 'models');
const STEM_MODEL_DEMUCS_FAST = (() => {
  const enc = path.join(STEM_MODELS_DIR, 'htdemucs_fast_encrypted.pt');
  const plain = path.join(STEM_MODELS_DIR, 'htdemucs_fast.pt');
  return fs.existsSync(enc) ? enc : plain;
})();

const BP_SDK_CALLBACK = 'http://localhost:61894/oauth/beatport';
const BP_SDK_CACHE    = path.join(os.homedir(), 'Music', 'DJ.Studio', '.beatport');

const ANALYZE_URL  = 'https://cf.dj.studio/mixedinkey/analyze';
const TARGET_SR    = 44100;

// Phrase model: DJ Studio also passes a remote-fallback URL. Local model is
// what's actually used; the URL is only consulted on local-load failure.
const PHRASE_REMOTE_URL = 'https://dc14f75a9b28a4bf09462409ca5fb34e1.clg07azjl.paperspacegradient.com/phrases';

// ── stdout protocol helpers ───────────────────────────────────────────────────

function emit(event, fields = {}) {
  process.stdout.write(JSON.stringify({ event, ...fields }) + '\n');
}
function logMsg(msg) { emit('log', { message: String(msg) }); }

// ── 1. Beatport SDK ───────────────────────────────────────────────────────────

let bpClient = null;

async function initSdk(stagingApi = false) {
  const { BeatportClient } = require(SDK_NODE);
  bpClient = BeatportClient;
  bpClient.initializeLog('/tmp/dj_studio_sdk.log', 1, 1000, true, true);
  bpClient.onStateChanged(state => logMsg(`bp-state ${state}`));
  const okInit = await bpClient.init(BP_SDK_CALLBACK, BP_SDK_CACHE, !!stagingApi);
  if (!okInit) throw new Error('BeatportClient.init returned false');
  const loginRes = await bpClient.login();
  if (!loginRes?.success) {
    throw new Error(`Beatport login failed: ${loginRes?.message || 'unknown'} (status=${loginRes?.status} state=${loginRes?.state})`);
  }
}

async function fetchFullTrackAudio(beatportId) {
  const info = await bpClient.getTrackAudioInformation(beatportId);
  const totalSamples = info?.totalSamples;
  const sampleRate   = info?.sampleRate || TARGET_SR;
  const channels     = info?.channels   || 2;
  if (!totalSamples) throw new Error(`no audio info for track ${beatportId}`);

  // getAudioSamples returns [Float32Array left, Float32Array right] (or [mono]).
  const channelArrays = await bpClient.getAudioSamples(beatportId, 0, totalSamples);
  if (!Array.isArray(channelArrays) || !channelArrays[0]) {
    throw new Error(`getAudioSamples returned no data for ${beatportId}`);
  }
  return { channels, sampleRate, totalSamples, channelArrays };
}

// Mix stereo → mono by averaging.
function toMono(channelArrays) {
  if (channelArrays.length === 1) return channelArrays[0];
  const left = channelArrays[0], right = channelArrays[1];
  const out = new Float32Array(left.length);
  for (let i = 0; i < left.length; i++) out[i] = (left[i] + right[i]) * 0.5;
  return out;
}

// Resample if needed (simple linear).
function resample(input, srIn, srOut) {
  if (srIn === srOut) return input;
  const ratio = srIn / srOut;
  const outLen = Math.floor(input.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const j = i * ratio;
    const j0 = Math.floor(j);
    const f = j - j0;
    out[i] = (input[j0] || 0) * (1 - f) + (input[j0 + 1] || 0) * f;
  }
  return out;
}

// ── 2. MIK WASM extractor (chromagram + energy + tempo + beatgrid + cuepoints) ──

let mikInstance = null;

async function getMikInstance() {
  if (mikInstance) return mikInstance;
  const KeyFeatureExtractor = require(MIK_WASM);
  mikInstance = await KeyFeatureExtractor();
  return mikInstance;
}

function getF64BlockArray(wasm, ptr) {
  const sizeBytes = new Int32Array(wasm.HEAPU8.slice(ptr, ptr + 8).buffer)[0];
  const count = sizeBytes / 8;
  const out = new Float64Array(count);
  if (count) out.set(new Float64Array(wasm.HEAPU8.slice(ptr + 8, ptr + 8 + sizeBytes).buffer));
  wasm._free(ptr);
  return out;
}
function getU8BlockArray(wasm, ptr) {
  const size = new Int32Array(wasm.HEAPU8.slice(ptr, ptr + 8).buffer)[0];
  const out = new Uint8Array(size);
  if (size) out.set(new Uint8Array(wasm.HEAPU8.slice(ptr + 8, ptr + 8 + size).buffer));
  wasm._free(ptr);
  return out;
}
function copyToWasm(wasm, arr) {
  const buf = wasm._malloc(arr.length * arr.BYTES_PER_ELEMENT);
  wasm.HEAPU8.set(new Uint8Array(arr.buffer, arr.byteOffset, arr.byteLength), buf);
  return buf;
}

async function runMikWasm(monoSamples, durationSec) {
  const wasm = await getMikInstance();

  const audioBuf  = copyToWasm(wasm, monoSamples);
  const emptyGrid = copyToWasm(wasm, new Float64Array(0));
  const mik = wasm._mik_analysis_new_with_beatgrid(TARGET_SR, emptyGrid, 0);
  wasm._free(emptyGrid);

  wasm._mik_analysis_add_audio(mik, audioBuf, 1, monoSamples.length);
  wasm._free(audioBuf);

  const keyFeatures = getF64BlockArray(wasm, wasm._mik_analysis_get_key_results(mik));

  const segCount = wasm._mik_analysis_get_energy_segment_count(mik);
  const energySegments = [];
  for (let i = 0; i < segCount; i++) {
    energySegments.push({
      StartTime:   wasm._mik_analysis_get_energy_segment_start_time(mik, i),
      EndTime:     wasm._mik_analysis_get_energy_segment_end_time(mik, i),
      VolumeRmsDb: wasm._mik_analysis_get_energy_segment_volume(mik, i),
      Features: Array.from(getF64BlockArray(wasm, wasm._mik_analysis_get_energy_segment_features(mik, i))),
    });
  }

  const adjustedBeatGrid = getF64BlockArray(wasm, wasm._mik_analysis_get_beatgrid(mik));
  const adjustedTempo    = wasm._mik_analysis_get_tempo(mik);
  const downbeatTime     = wasm._mik_analysis_get_downbeat_time(mik);
  const cueStart         = wasm._mik_analysis_get_cue_point_start_beat(mik);
  const cueData          = getU8BlockArray(wasm, wasm._mik_analysis_get_cue_point_data(mik));

  wasm._mik_analysis_delete(mik);

  return {
    requestObject: {
      VIPCode: '', ProductName: 'Mixed In Key', ProductVersion: '10.0.0.0', Platform: 'Mac',
      Segments: [{ StartTime: 0, EndTime: durationSec, PitchProbabilities: null, Features: keyFeatures.length ? Array.from(keyFeatures) : null }],
      KeyAlgorithmVersion: '94', EnergyAlgorithmVersion: '2', EnergySegmentData: energySegments,
      DurationInSeconds: durationSec, EliteData: null,
      CuePointAlgorithmVersion: 3,
      CuePointData: cueData.length ? Buffer.from(cueData).toString('base64') : null,
      CuePointStartBeat: cueStart,
      Tempo: adjustedTempo, DownbeatTime: downbeatTime, FingerprintHash: null,
    },
    adjustedTempo, downbeatTime, cuePointStartBeat: cueStart,
    energySegmentCount: energySegments.length,
  };
}

// ── 3. cf.dj.studio classifier (final mikKey + mikEnergy + EnergyLevelSegments) ──

async function callClassifier(requestObject, accessJwt) {
  // cf.dj.studio occasionally times out or 5xx's on long payloads. Retry with
  // exponential backoff (1s, 3s, 9s) before giving up. 401/403 are not retried
  // — those are auth errors, not transient.
  const body = JSON.stringify(requestObject);
  const maxAttempts = 4;
  let lastErr = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      const ac = new AbortController();
      const timeoutMs = 90000; // 90s — full track features can take a while
      const timer = setTimeout(() => ac.abort(), timeoutMs);
      let r;
      try {
        r = await fetch(ANALYZE_URL, {
          method: 'POST',
          headers: { 'Authorization': `Bearer ${accessJwt}`, 'Content-Type': 'application/json', 'Accept': 'application/json' },
          body,
          signal: ac.signal,
        });
      } finally {
        clearTimeout(timer);
      }
      if (r.status === 200) {
        return { ok: true, status: 200, body: await r.json(), attempts: attempt };
      }
      const txt = await r.text().catch(() => '');
      // Non-retryable: 4xx auth / bad request
      if (r.status === 401 || r.status === 403 || r.status === 400) {
        return { ok: false, status: r.status, body: txt, attempts: attempt };
      }
      lastErr = `HTTP ${r.status}: ${txt.slice(0, 200)}`;
    } catch (e) {
      lastErr = e.name === 'AbortError' ? 'timeout (90s)' : (e.message || String(e));
    }
    // Backoff before next attempt (skip after the last attempt)
    if (attempt < maxAttempts) {
      const delay = Math.pow(3, attempt - 1) * 1000;  // 1s, 3s, 9s
      logMsg(`classifier attempt ${attempt} failed (${lastErr}), retrying in ${delay/1000}s…`);
      await new Promise((res) => setTimeout(res, delay));
    }
  }
  return { ok: false, status: 0, body: lastErr || 'unknown error', attempts: maxAttempts };
}

// ── 4. ai-beatgrid (precise beats + phrases) ──────────────────────────────────

let beatgridAddon = null;
function getBeatgridAddon() {
  if (beatgridAddon) return beatgridAddon;
  beatgridAddon = require(BEATGRID_NODE);
  if (typeof beatgridAddon.enableLogging === 'function') beatgridAddon.enableLogging(false);
  return beatgridAddon;
}

async function runBeatgrid(monoSamples) {
  const addon = getBeatgridAddon();
  const result = await addon.processAsync(monoSamples, BEATGRID_MODEL_BEATS, 1);
  return {
    detected_key: result.key || null,
    beats: (result.beats || []).map(b => ({ time: b.time, position: b.position })),
    beats_aligned: (result.beats_aligned || []).map(b => ({ time: b.time, position: b.position })),
  };
}

// ML phrase detection (ai-beatgrid model_phrases.pt) is dormant in DJ Studio's
// current renderer (BeatgridChannelClient doesn't expose processPhrases) and
// the remote-fallback URL is dead (404). Real DJ Studio audio-library-table
// entries also have phraseData=[] but populate beatData[].phraseNr using a
// deterministic 8-bar rule. We do the same in Python (see _shape_result) and
// skip the ML call entirely here.
async function runPhraseDetect(_monoSamples, _beatTimes) { return null; }

// ── 5. ai-stems (Demucs Fast: vocals + bass + drums + other) ──────────────────

let stemSeparator = null;
let stemModelLoaded = false;

async function loadStems() {
  if (stemSeparator && stemModelLoaded) return stemSeparator;
  const addon = require(STEMS_NODE);
  if (!stemSeparator) {
    stemSeparator = new addon.AudioSeparator();
    if (typeof stemSeparator.onLog === 'function') stemSeparator.onLog((lvl, msg) => logMsg(`stems[${lvl}] ${msg}`));
    if (typeof stemSeparator.enableConsoleLogging === 'function') stemSeparator.enableConsoleLogging(false);
  }
  if (!stemModelLoaded) {
    const r = stemSeparator.loadModel('demucsFast', STEM_MODEL_DEMUCS_FAST, 'auto');
    if (!r.loaded) throw new Error(`stem model load failed: ${r.message}`);
    stemModelLoaded = true;
  }
  return stemSeparator;
}

async function runStems(monoSamples) {
  const sep = await loadStems();
  const t0 = Date.now();
  const r = await sep.processDemucsFast({ mono: monoSamples }, STEM_MODEL_DEMUCS_FAST, { device: 'auto' });
  const ms = Date.now() - t0;
  // Demucs Fast mono output is { vocals, bass, drums, other } — each Float32Array
  // (or [Float32Array] one-element array). Normalise to plain Float32Array.
  const norm = (s) => (s instanceof Float32Array ? s : (Array.isArray(s) && s[0] instanceof Float32Array ? s[0] : null));
  return {
    process_time_ms: ms,
    stems: {
      vocals: norm(r.stems?.vocals),
      bass:   norm(r.stems?.bass),
      drums:  norm(r.stems?.drums),
      other:  norm(r.stems?.other),
    },
  };
}

// Compress a stem to DJ Studio's compressedAudioView* binary format:
//   2-byte header + N records of 8 bytes each
//   Each record: bytes[0..5] = 0, bytes[6..7] = uint16_LE(amplitude)
// Bucket size: 1024 samples (~23ms at 44.1k) — matches DJ Studio's record density.
// Returns { compressed_b64, avg_rms, peak_rms } so the caller can store summary
// metrics without re-decoding the binary.
const BUCKET_SAMPLES = 1024;
function compressStemToView(stemSamples) {
  if (!stemSamples) return { compressed_b64: null, avg_rms: null, peak_rms: null, rms_per_bucket: null };
  const n = Math.floor(stemSamples.length / BUCKET_SAMPLES);
  const out = Buffer.alloc(2 + n * 8);
  out.writeUInt16LE(0xFFFF, 0);
  // We also return the per-bucket RMS series so the Python orchestrator can
  // compute per-phrase stats without re-decoding the binary view.
  // Quantised to uint16 (same precision as DJ Studio's stored format) to keep
  // the IPC payload small — 14k buckets/track × 2 bytes × 4 stems ≈ 110KB.
  const buckets = new Uint16Array(n);
  let sumRms = 0;
  let peakRms = 0;
  for (let i = 0; i < n; i++) {
    let sumSq = 0;
    const base = i * BUCKET_SAMPLES;
    for (let j = 0; j < BUCKET_SAMPLES; j++) {
      const s = stemSamples[base + j];
      sumSq += s * s;
    }
    const rms = Math.sqrt(sumSq / BUCKET_SAMPLES);
    sumRms += rms;
    if (rms > peakRms) peakRms = rms;
    let amp = Math.round(rms * 65535);
    if (amp < 0) amp = 0;
    if (amp > 65535) amp = 65535;
    out.writeUInt16LE(amp, 2 + i * 8 + 6);
    buckets[i] = amp;
  }
  return {
    compressed_b64: out.toString('base64'),
    avg_rms: n ? sumRms / n : 0,
    peak_rms: peakRms,
    // Send the bucket series back as base64 of the uint16 LE buffer — Python
    // can numpy.frombuffer it (or struct.unpack) without parsing the 8-byte
    // record format we use for DJ Studio's on-disk file.
    rms_per_bucket_b64: Buffer.from(buckets.buffer, buckets.byteOffset, buckets.byteLength).toString('base64'),
  };
}

// ── Per-track orchestration ───────────────────────────────────────────────────

async function analyzeTrack(beatportId, accessJwt) {
  const tStart = Date.now();
  const audio = await fetchFullTrackAudio(beatportId);
  const tFetch = Date.now() - tStart;

  const mono = audio.sampleRate === TARGET_SR
    ? toMono(audio.channelArrays)
    : resample(toMono(audio.channelArrays), audio.sampleRate, TARGET_SR);
  const durationSec = mono.length / TARGET_SR;
  // Surface audio dimensions up-front so partial-output diagnostics later in
  // this run can be correlated with track length / source quality. Tracks
  // under ~30s typically can't produce reliable beats or stems.
  logMsg(`bp:${beatportId} audio: ${durationSec.toFixed(1)}s  src_sr=${audio.sampleRate}  ch=${audio.channels}  mono_samples=${mono.length}`);
  if (durationSec < 30) {
    logMsg(`bp:${beatportId} short audio (${durationSec.toFixed(1)}s) — beatgrid/stems may return empty`);
  }

  // 1. MIK WASM
  const tMikStart = Date.now();
  const mikLocal = await runMikWasm(mono, durationSec);
  const tMik = Date.now() - tMikStart;

  // 2. cf.dj.studio classifier
  const tSrvStart = Date.now();
  const srv = await callClassifier(mikLocal.requestObject, accessJwt);
  const tSrv = Date.now() - tSrvStart;

  // 3. ai-beatgrid beats + key
  const tBgStart = Date.now();
  const beatgrid = await runBeatgrid(mono);
  const tBg = Date.now() - tBgStart;
  if (!beatgrid.beats || beatgrid.beats.length === 0) {
    logMsg(`bp:${beatportId} ai-beatgrid returned 0 beats (key=${beatgrid.detected_key || 'none'}) — track will be flagged as incomplete on Python side`);
  }

  // 4. ai-beatgrid phrases (best-effort)
  const tPhStart = Date.now();
  const phrases = await runPhraseDetect(mono, beatgrid.beats.map(b => b.time));
  const tPh = Date.now() - tPhStart;

  // 5. ai-stems
  const tStStart = Date.now();
  const stems = await runStems(mono);
  const tSt = Date.now() - tStStart;
  const emptyStems = ['vocals', 'drums', 'bass', 'other'].filter(k => !stems.stems?.[k]);
  if (emptyStems.length) {
    logMsg(`bp:${beatportId} ai-stems returned empty for: ${emptyStems.join(',')} — track will be flagged as incomplete on Python side`);
  }

  // 6. compressed views + per-stem RMS metrics
  const compressed = {
    vocals: compressStemToView(stems.stems.vocals),
    drums:  compressStemToView(stems.stems.drums),
    bass:   compressStemToView(stems.stems.bass),
    other:  compressStemToView(stems.stems.other),
  };
  const stem_metrics = {
    vocals: { avg_rms: compressed.vocals.avg_rms, peak_rms: compressed.vocals.peak_rms },
    drums:  { avg_rms: compressed.drums.avg_rms,  peak_rms: compressed.drums.peak_rms },
    bass:   { avg_rms: compressed.bass.avg_rms,   peak_rms: compressed.bass.peak_rms },
    other:  { avg_rms: compressed.other.avg_rms,  peak_rms: compressed.other.peak_rms },
  };

  return {
    beatport_id: beatportId,
    duration_sec: durationSec,
    sample_rate: audio.sampleRate,
    channels: audio.channels,
    timing_ms: { fetch: tFetch, mik: tMik, server: tSrv, beatgrid: tBg, phrases: tPh, stems: tSt, total: Date.now() - tStart },
    wasm: {
      tempo: mikLocal.adjustedTempo,
      downbeat_time: mikLocal.downbeatTime,
      cue_point_start_beat: mikLocal.cuePointStartBeat,
      energy_segment_count: mikLocal.energySegmentCount,
    },
    server: srv.ok ? { ok: true, body: srv.body } : { ok: false, status: srv.status, body: srv.body },
    beatgrid: {
      detected_key: beatgrid.detected_key,
      beats: beatgrid.beats,
      beats_aligned: beatgrid.beats_aligned,
    },
    phrases: phrases || null,
    stems_compressed_b64: {
      vocals: compressed.vocals.compressed_b64,
      drums:  compressed.drums.compressed_b64,
      bass:   compressed.bass.compressed_b64,
      other:  compressed.other.compressed_b64,
    },
    // Per-bucket RMS for each stem (uint16 LE, base64). 1024-sample buckets at
    // 44.1k = ~23ms per bucket, ~43 buckets/sec. Python downsamples + slices
    // into per-second curves and per-energy-segment averages for analysis_json.
    // Same precision as DJ Studio's compressed view (uint16 / 65535 = float).
    stems_rms_per_bucket_b64: {
      vocals: compressed.vocals.rms_per_bucket_b64,
      drums:  compressed.drums.rms_per_bucket_b64,
      bass:   compressed.bass.rms_per_bucket_b64,
      other:  compressed.other.rms_per_bucket_b64,
    },
    stems_bucket_samples: BUCKET_SAMPLES,
    stems_target_sr: TARGET_SR,
    stem_metrics,
    stems_process_time_ms: stems.process_time_ms,
  };
}

// ── stdin command loop ────────────────────────────────────────────────────────

const rl = readline.createInterface({ input: process.stdin, terminal: false });
let djsAccessJwt = '';
let initialised = false;

rl.on('line', async (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch (e) {
    emit('error', { message: `bad JSON line: ${e.message}` });
    return;
  }
  if (!msg.cmd) return;

  try {
    if (msg.cmd === 'init') {
      djsAccessJwt = msg.djsAccessJwt || '';
      await initSdk(!!msg.stagingApi);
      // Pre-load the stem model (heavy — better here than on first track).
      try { await loadStems(); } catch (e) { logMsg(`stem preload: ${e.message}`); }
      initialised = true;
      emit('ready');
    } else if (msg.cmd === 'analyze') {
      if (!initialised) { emit('error', { beatport_id: msg.beatport_id, message: 'not initialised' }); return; }
      try {
        const result = await analyzeTrack(msg.beatport_id, djsAccessJwt);
        emit('analysis', { beatport_id: msg.beatport_id, result });
      } catch (e) {
        emit('error', { beatport_id: msg.beatport_id, message: e.message });
      }
    } else if (msg.cmd === 'setAccessJwt') {
      // Mid-run JWT refresh — Python decrypts a fresh token and pushes it down.
      // Subsequent analyze calls pick it up automatically.
      djsAccessJwt = msg.djsAccessJwt || '';
      emit('jwtUpdated');
    } else if (msg.cmd === 'exit') {
      try { if (bpClient) await bpClient.release(); } catch {}
      emit('exit');
      process.exit(0);
    }
  } catch (e) {
    emit('error', { message: e.message });
  }
});

rl.on('close', async () => {
  try { if (bpClient) await bpClient.release(); } catch {}
  process.exit(0);
});

emit('log', { message: 'helper started, waiting for init…' });
