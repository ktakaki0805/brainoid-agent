/** Canonical, memory-only PCM contract shared by capture and playback. */
export const SAMPLE_RATE = 16_000;
export const MAX_FRAMES = SAMPLE_RATE * 60;
export const MAX_WAV_BYTES = 5 * 1024 * 1024;
export const MAX_CANONICAL_BYTES = 44 + MAX_FRAMES * 2;

export function encodeWav(samples: Int16Array): Uint8Array<ArrayBuffer> {
  if (!samples.length || samples.length > MAX_FRAMES) throw new Error("Invalid audio length");
  const bytes = new Uint8Array(44 + samples.length * 2),
    view = new DataView(bytes.buffer);
  const ascii = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i++) bytes[offset + i] = text.charCodeAt(i);
  };
  ascii(0, "RIFF");
  view.setUint32(4, bytes.length - 8, true);
  ascii(8, "WAVEfmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, SAMPLE_RATE, true);
  view.setUint32(28, SAMPLE_RATE * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  ascii(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) view.setInt16(44 + i * 2, samples[i] ?? 0, true);
  return bytes;
}

/** Validate RIFF including optional metadata and PCM fmt extension, matching the API. */
function pcmRange(bytes: Uint8Array): [number, number] {
  if (bytes.length < 12 || bytes.length > MAX_WAV_BYTES) throw new Error("Invalid audio length");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const text = (a: number, b: number) => String.fromCharCode(...bytes.subarray(a, b));
  if (
    text(0, 4) !== "RIFF" ||
    text(8, 12) !== "WAVE" ||
    view.getUint32(4, true) !== bytes.length - 8
  )
    throw new Error("Invalid audio format");
  let offset = 12,
    chunks = 0,
    format = false,
    range: [number, number] | undefined;
  while (offset < bytes.length) {
    if (++chunks > 1024 || offset + 8 > bytes.length) throw new Error("Invalid audio format");
    const kind = text(offset, offset + 4),
      size = view.getUint32(offset + 4, true),
      start = offset + 8,
      end = start + size;
    if (end + (size % 2) > bytes.length) throw new Error("Invalid audio format");
    if (kind === "fmt ") {
      if (
        format ||
        range ||
        ![16, 18].includes(size) ||
        view.getUint16(start, true) !== 1 ||
        view.getUint16(start + 2, true) !== 1 ||
        view.getUint32(start + 4, true) !== SAMPLE_RATE ||
        view.getUint32(start + 8, true) !== 32000 ||
        view.getUint16(start + 12, true) !== 2 ||
        view.getUint16(start + 14, true) !== 16 ||
        (size === 18 && view.getUint16(end - 2, true) !== 0)
      )
        throw new Error("Invalid audio format");
      format = true;
    } else if (kind === "data") {
      if (!format || range || !size || size % 2 || size / 2 > MAX_FRAMES)
        throw new Error("Invalid audio length");
      range = [start, end];
    }
    offset = end + (size % 2);
  }
  if (!range || !bytes.subarray(...range).some((value) => value !== 0))
    throw new Error("Silent audio");
  return range;
}
export function validateWav(bytes: Uint8Array): number {
  const [start, end] = pcmRange(bytes);
  return (end - start) / 2;
}
/** Native playback accepts a compact header; strip optional provider metadata in memory. */
export function canonicalWav(bytes: Uint8Array): Uint8Array<ArrayBuffer> {
  const [start, end] = pcmRange(bytes),
    view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const samples = new Int16Array((end - start) / 2);
  for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(start + i * 2, true);
  try {
    return encodeWav(samples);
  } finally {
    samples.fill(0);
  }
}

export function fromBase64(encoded: string): Uint8Array<ArrayBuffer> {
  if (encoded.length > Math.ceil(MAX_CANONICAL_BYTES / 3) * 4)
    throw new Error("Invalid audio length");
  return Uint8Array.from(atob(encoded), (value) => value.charCodeAt(0));
}
export function toBase64(bytes: Uint8Array): string {
  let encoded = "";
  for (let i = 0; i < bytes.length; i += 8192)
    encoded += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(encoded);
}
