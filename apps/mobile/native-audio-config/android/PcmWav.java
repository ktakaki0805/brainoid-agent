package dev.brainoid.agent.audio;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

/** Pure PCM contract. No file, codec, network or Android dependency. */
public final class PcmWav {
    public static final int MAX_FRAMES = 960000;
    public static final int MAX_BYTES = 44 + MAX_FRAMES * 2;
    private PcmWav() {}
    public static byte[] encode(short[] samples, int count) {
        if (count < 1 || count > MAX_FRAMES || count > samples.length) throw new IllegalArgumentException();
        ByteBuffer b = ByteBuffer.allocate(44 + count * 2).order(ByteOrder.LITTLE_ENDIAN);
        b.put("RIFF".getBytes(StandardCharsets.US_ASCII)).putInt(b.capacity() - 8);
        b.put("WAVEfmt ".getBytes(StandardCharsets.US_ASCII)).putInt(16).putShort((short) 1).putShort((short) 1);
        b.putInt(16000).putInt(32000).putShort((short) 2).putShort((short) 16);
        b.put("data".getBytes(StandardCharsets.US_ASCII)).putInt(count * 2);
        for (int i = 0; i < count; i++) b.putShort(samples[i]);
        return b.array();
    }
    public static byte[] decode(byte[] wav) {
        if (wav.length < 46 || wav.length > MAX_BYTES || wav.length % 2 != 0) throw new IllegalArgumentException();
        ByteBuffer b = ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN);
        if (b.getInt(0) != 0x46464952 || b.getInt(4) != wav.length - 8 || b.getInt(8) != 0x45564157 ||
            b.getInt(12) != 0x20746d66 || b.getInt(16) != 16 || b.getShort(20) != 1 || b.getShort(22) != 1 ||
            b.getInt(24) != 16000 || b.getInt(28) != 32000 || b.getShort(32) != 2 || b.getShort(34) != 16 ||
            b.getInt(36) != 0x61746164 || b.getInt(40) != wav.length - 44) throw new IllegalArgumentException();
        return Arrays.copyOfRange(wav, 44, wav.length);
    }
}
