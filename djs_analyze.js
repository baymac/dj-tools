#!/usr/bin/env node
/**
 * Thin wrapper around DJ Studio's bundled ai-beatgrid addon.
 *
 * Usage:
 *   node djs_analyze.js <pcm_f32le_file> [folds]
 *
 * Input:  raw 44100 Hz mono float32-LE PCM (piped or file path)
 * Output: one JSON line  { key, camelotKey, mikKeyNr, bpm }
 */

const fs   = require('fs');
const path = require('path');

const DJS_UNPACKED = '/Applications/DJ.Studio.app/Contents/Resources/app.asar.unpacked';
const ADDON_PATH   = path.join(DJS_UNPACKED, 'node_modules/@appmachine/ai-beatgrid/build/Release/ai-beatgrid.node');
const MODEL_PATH   = path.join(DJS_UNPACKED, 'node_modules/@appmachine/ai-beatgrid/build/Release/model_fold_0.pt');

// Matches DJ Studio's beatgridWorker.js KEY_MAP (0-indexed)
const KEY_MAP = {
    'C major':0,'D flat major':1,'D major':2,'E flat major':3,'E major':4,
    'F major':5,'G flat major':6,'G major':7,'A flat major':8,'A major':9,
    'B flat major':10,'B major':11,
    'A minor':12,'B flat minor':13,'B minor':14,'C minor':15,'D flat minor':16,
    'D minor':17,'E flat minor':18,'E minor':19,'F minor':20,'F sharp minor':21,
    'G minor':22,'A flat minor':23,
};
const CAMELOT = [
    '8B','3B','10B','5B','12B','7B','2B','9B','4B','11B','6B','1B',
    '5A','12A','7A','2A','9A','4A','11A','6A','1A','8A','3A','10A',
];

function beatsToAvgBpm(beatTimes) {
    if (!beatTimes || beatTimes.length < 2) return 0;
    let sum = 0, n = 0;
    for (let i = 1; i < beatTimes.length; i++) {
        const interval = beatTimes[i] - beatTimes[i - 1];
        if (interval > 0.05) { sum += 60 / interval; n++; }  // skip zero/duplicate intervals
    }
    return n > 0 ? Math.round(sum / n) : 0;
}

(async () => {
    const pcmPath = process.argv[2];
    const folds   = parseInt(process.argv[3] || '1', 10);

    if (!pcmPath) {
        process.stderr.write('Usage: node djs_analyze.js <pcm_f32le_file>\n');
        process.exit(1);
    }

    const addon = require(ADDON_PATH);
    addon.enableLogging(false);

    const buf     = fs.readFileSync(pcmPath);
    const samples = new Float32Array(buf.buffer, buf.byteOffset, buf.length / 4);

    const result = await addon.processAsync(samples, MODEL_PATH, folds);

    // key detection was removed from processAsync in ai-beatgrid v1.2.x;
    // key is now detected via Python/librosa in beatport_analyze.py
    const bpm = beatsToAvgBpm(result.beatTimes);

    process.stdout.write(JSON.stringify({ bpm }) + '\n');
    process.exit(0);
})();
