import dev.brainoid.agent.audio.PcmWav;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;

public final class PcmWavFixture {
    public static void main(String[] args) {
        short[] pcm = { Short.MIN_VALUE, -1, 0, 1, Short.MAX_VALUE };
        byte[] wav = PcmWav.encode(pcm, pcm.length);
        ByteBuffer b = ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN);
        if (wav.length != 54 || b.getInt(24) != 16000 || b.getShort(22) != 1 || b.getShort(34) != 16) throw new AssertionError();
        for (int i = 0; i < pcm.length; i++) if (b.getShort(44 + i * 2) != pcm[i]) throw new AssertionError();
        if (!Arrays.equals(PcmWav.decode(wav), Arrays.copyOfRange(wav, 44, wav.length))) throw new AssertionError();
        byte[] max = PcmWav.encode(new short[PcmWav.MAX_FRAMES], PcmWav.MAX_FRAMES);
        if (max.length != 1920044 || max.length > 5 * 1024 * 1024) throw new AssertionError();
        rejects(() -> PcmWav.encode(new short[1], 0));
        rejects(() -> PcmWav.encode(new short[PcmWav.MAX_FRAMES + 1], PcmWav.MAX_FRAMES + 1));
        wav[24] = 0; rejects(() -> PcmWav.decode(wav));
        System.out.println("Android PCM fixture: header, signed LE samples, 60s/5MiB bounds, malformed rejection passed");
    }
    private static void rejects(Runnable work) {
        try { work.run(); } catch (IllegalArgumentException expected) { return; }
        throw new AssertionError("Expected rejection");
    }
}
