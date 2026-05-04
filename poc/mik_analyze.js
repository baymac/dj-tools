#!/usr/bin/env node
/**
 * Run MIK analysis on a WAV/MP3 file using DJ Studio's bundled WASM extractor
 * + DJ Studio's classifier server.
 *
 * Usage:  node mik_analyze.js <audio_path> <access_token>
 * Output: JSON to stdout with the analyze response + locally-extracted fields.
 *
 * This script intentionally has no npm dependencies — uses Node built-ins +
 * the WASM module DJ Studio ships at:
 *   /Applications/DJ.Studio.app/Contents/Resources/public/5/key-feature-extractor.js
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const DJ_STUDIO_WASM = '/Applications/DJ.Studio.app/Contents/Resources/public/5/key-feature-extractor.js';
const ANALYZE_URL = 'https://cf.dj.studio/mixedinkey/analyze';
const TARGET_SAMPLE_RATE = 44100;

// ─── 1. Decode audio → Float32Array mono @ 44100Hz ────────────────────────────

function decodeAudio(audioPath) {
  // Use ffmpeg to decode any input to raw float32 mono @ 44100Hz.
  // ffmpeg is available via macOS Homebrew; if not we'll fall back to a built-in
  // WAV parser below.
  const ffmpeg = spawnSync('ffmpeg', [
    '-loglevel', 'error',
    '-i', audioPath,
    '-f', 'f32le',
    '-ar', String(TARGET_SAMPLE_RATE),
    '-ac', '1',
    '-',
  ], { stdio: ['ignore', 'pipe', 'pipe'], maxBuffer: 256 * 1024 * 1024 });

  if (ffmpeg.status === 0 && ffmpeg.stdout && ffmpeg.stdout.length > 0) {
    const buf = ffmpeg.stdout;
    const samples = new Float32Array(buf.buffer, buf.byteOffset, buf.byteLength / 4);
    // Detach from underlying Node Buffer so the WASM can own it.
    return new Float32Array(samples);
  }

  // Fallback: if ffmpeg isn't available, parse a 16-bit PCM WAV directly.
  const data = fs.readFileSync(audioPath);
  if (data.slice(0, 4).toString() !== 'RIFF' || data.slice(8, 12).toString() !== 'WAVE') {
    throw new Error(
      `ffmpeg failed (status ${ffmpeg.status}: ${ffmpeg.stderr?.toString().trim()}) ` +
      `and input is not a RIFF WAV — install ffmpeg via "brew install ffmpeg".`
    );
  }
  return decodeRiffWav(data);
}

function decodeRiffWav(buf) {
  // Walk chunks until we find "fmt " and "data".
  let off = 12;
  let format, channels, sampleRate, bitsPerSample, dataOff, dataSize;
  while (off < buf.length - 8) {
    const id = buf.slice(off, off + 4).toString();
    const size = buf.readUInt32LE(off + 4);
    if (id === 'fmt ') {
      format = buf.readUInt16LE(off + 8);
      channels = buf.readUInt16LE(off + 10);
      sampleRate = buf.readUInt32LE(off + 12);
      bitsPerSample = buf.readUInt16LE(off + 22);
    } else if (id === 'data') {
      dataOff = off + 8;
      dataSize = size;
    }
    off += 8 + size;
  }
  if (format !== 1 || bitsPerSample !== 16) {
    throw new Error(`Unsupported WAV (format=${format}, bits=${bitsPerSample}). Install ffmpeg.`);
  }
  const sampleCount = dataSize / (channels * 2);
  const out = new Float32Array(sampleCount);
  for (let i = 0; i < sampleCount; i++) {
    let acc = 0;
    for (let c = 0; c < channels; c++) {
      acc += buf.readInt16LE(dataOff + (i * channels + c) * 2);
    }
    out[i] = (acc / channels) / 32768;
  }
  // Resample if needed (the WASM expects 44100; no-op when source already is)
  if (sampleRate !== TARGET_SAMPLE_RATE) {
    return resampleLinear(out, sampleRate, TARGET_SAMPLE_RATE);
  }
  return out;
}

function resampleLinear(input, srIn, srOut) {
  const ratio = srIn / srOut;
  const outLen = Math.floor(input.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const srcF = i * ratio;
    const j = Math.floor(srcF);
    const frac = srcF - j;
    out[i] = (input[j] || 0) * (1 - frac) + (input[j + 1] || 0) * frac;
  }
  return out;
}

// ─── 2. WASM helpers (mirror DJ Studio's mixed-in-key-worker-node.js) ─────────

function getMemoryBlockFromArray(wasm, arr) {
  const buf = wasm._malloc(arr.length * arr.BYTES_PER_ELEMENT);
  wasm.HEAPU8.set(new Uint8Array(arr.buffer, arr.byteOffset, arr.byteLength), buf);
  return buf;
}

function getFloat64ArrayFromMemoryBlock(wasm, block) {
  const sizeBytes = new Int32Array(wasm.HEAPU8.slice(block, block + 8).buffer)[0];
  const count = sizeBytes / 8;
  const out = new Float64Array(count);
  if (count) {
    out.set(new Float64Array(wasm.HEAPU8.slice(block + 8, block + 8 + sizeBytes).buffer));
  }
  wasm._free(block);
  return out;
}

function getUInt8ArrayFromMemoryBlock(wasm, block) {
  const size = new Int32Array(wasm.HEAPU8.slice(block, block + 8).buffer)[0];
  const out = new Uint8Array(size);
  if (size) {
    out.set(new Uint8Array(wasm.HEAPU8.slice(block + 8, block + 8 + size).buffer));
  }
  wasm._free(block);
  return out;
}

function getEnergySegments(wasm, mik) {
  const n = wasm._mik_analysis_get_energy_segment_count(mik);
  const segments = [];
  for (let i = 0; i < n; i++) {
    segments.push({
      StartTime: wasm._mik_analysis_get_energy_segment_start_time(mik, i),
      EndTime: wasm._mik_analysis_get_energy_segment_end_time(mik, i),
      VolumeRmsDb: wasm._mik_analysis_get_energy_segment_volume(mik, i),
      Features: Array.from(getFloat64ArrayFromMemoryBlock(
        wasm, wasm._mik_analysis_get_energy_segment_features(mik, i)
      )),
    });
  }
  return segments;
}

// ─── 3. Run WASM analysis ─────────────────────────────────────────────────────

async function runWasmAnalysis(samples, durationSec) {
  const KeyFeatureExtractor = require(DJ_STUDIO_WASM);
  const wasm = await KeyFeatureExtractor();

  const audioBuf = getMemoryBlockFromArray(wasm, samples);
  const beatGridBuf = getMemoryBlockFromArray(wasm, new Float64Array(0));

  const mik = wasm._mik_analysis_new_with_beatgrid(TARGET_SAMPLE_RATE, beatGridBuf, 0);
  wasm._free(beatGridBuf);

  wasm._mik_analysis_add_audio(mik, audioBuf, 1, samples.length);
  wasm._free(audioBuf);

  const keyFeatures = getFloat64ArrayFromMemoryBlock(wasm, wasm._mik_analysis_get_key_results(mik));
  const energySegments = getEnergySegments(wasm, mik);
  const adjustedBeatGrid = getFloat64ArrayFromMemoryBlock(wasm, wasm._mik_analysis_get_beatgrid(mik));
  const adjustedTempo = wasm._mik_analysis_get_tempo(mik);
  const downbeatTime = wasm._mik_analysis_get_downbeat_time(mik);
  const cuePointStartBeat = wasm._mik_analysis_get_cue_point_start_beat(mik);
  const cuePointDataRaw = getUInt8ArrayFromMemoryBlock(wasm, wasm._mik_analysis_get_cue_point_data(mik));

  wasm._mik_analysis_delete(mik);

  const requestObject = {
    VIPCode: '',
    ProductName: 'Mixed In Key',
    ProductVersion: '10.0.0.0',
    Platform: 'Mac',
    Segments: [{
      StartTime: 0,
      EndTime: durationSec,
      PitchProbabilities: null,
      Features: keyFeatures.length ? Array.from(keyFeatures) : null,
    }],
    KeyAlgorithmVersion: '94',
    EnergyAlgorithmVersion: '2',
    EnergySegmentData: energySegments,
    DurationInSeconds: durationSec,
    EliteData: null,
    CuePointAlgorithmVersion: 3,
    CuePointData: cuePointDataRaw.length ? Buffer.from(cuePointDataRaw).toString('base64') : null,
    CuePointStartBeat: cuePointStartBeat,
    Tempo: adjustedTempo,
    DownbeatTime: downbeatTime,
    FingerprintHash: null,
  };

  return {
    adjustedBeatGrid: Array.from(adjustedBeatGrid),
    adjustedTempo,
    downbeatTime,
    cuePointStartBeat,
    requestObject,
    energySegmentCount: energySegments.length,
  };
}

// ─── 4. POST to cf.dj.studio/mixedinkey/analyze ───────────────────────────────

async function callAnalyzeServer(requestObject, accessToken) {
  const r = await fetch(ANALYZE_URL, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
      'Accept': 'application/json',
    },
    body: JSON.stringify(requestObject),
  });
  if (r.status !== 200) {
    return { httpStatus: r.status, body: await r.text() };
  }
  return { httpStatus: 200, body: await r.json() };
}

// ─── main ─────────────────────────────────────────────────────────────────────

(async () => {
  const [, , audioPath, accessToken] = process.argv;
  if (!audioPath || !accessToken) {
    console.error('Usage: node mik_analyze.js <audio_path> <access_token>');
    process.exit(1);
  }

  const t0 = Date.now();
  const samples = decodeAudio(audioPath);
  const durationSec = samples.length / TARGET_SAMPLE_RATE;
  const tDecode = Date.now() - t0;

  const t1 = Date.now();
  const wasmResult = await runWasmAnalysis(samples, durationSec);
  const tWasm = Date.now() - t1;

  const t2 = Date.now();
  const serverResult = await callAnalyzeServer(wasmResult.requestObject, accessToken);
  const tServer = Date.now() - t2;

  const out = {
    ok: serverResult.httpStatus === 200,
    timing_ms: { decode: tDecode, wasm: tWasm, server: tServer, total: Date.now() - t0 },
    audio: { samples: samples.length, duration_sec: durationSec },
    wasm: {
      tempo: wasmResult.adjustedTempo,
      downbeat_time: wasmResult.downbeatTime,
      cue_point_start_beat: wasmResult.cuePointStartBeat,
      beat_grid_length: wasmResult.adjustedBeatGrid.length,
      energy_segment_count: wasmResult.energySegmentCount,
    },
    server: serverResult,
  };

  process.stdout.write(JSON.stringify(out, null, 2) + '\n');
})().catch(e => {
  console.error(e.stack || e.message || e);
  process.exit(2);
});
