package dev.brainoid.agent.audio;

import android.Manifest;
import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioDeviceCallback;
import android.media.AudioDeviceInfo;
import android.media.AudioFocusRequest;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;
import java.util.Arrays;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/** App-local Capacitor 8 plugin. All audio stays in bounded memory. */
@CapacitorPlugin(name = "BrainoidAudio", permissions = {
    @Permission(alias = "microphone", strings = { Manifest.permission.RECORD_AUDIO })
})
public final class BrainoidAudioPlugin extends Plugin {
    private final ScheduledExecutorService clock = Executors.newSingleThreadScheduledExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());
    private long floor = 0, active = 0;
    private boolean foreground = true;
    private AudioRecord recorder;
    private AudioTrack player;
    private short[] samples;
    private int count;
    private PluginCall playCall;
    private ScheduledFuture<?> recordDeadline, leaseDeadline, playDeadline;
    private AudioManager manager;
    private AudioFocusRequest focus;
    private final AudioDeviceCallback routes = new AudioDeviceCallback() {
        @Override public void onAudioDevicesRemoved(AudioDeviceInfo[] devices) {
            synchronized (BrainoidAudioPlugin.this) { if (recorder != null || player != null) interrupt("interrupted"); }
        }
    };
    @Override public void load() {
        manager = (AudioManager) getContext().getSystemService(Context.AUDIO_SERVICE);
        manager.registerAudioDeviceCallback(routes, main);
    }
    private Double number(PluginCall call, String key) {
        Object value = call.getData().opt(key);
        return value instanceof Number ? ((Number) value).doubleValue() : null;
    }
    private long generation(PluginCall call) {
        Double value = number(call, "generation");
        return value == null || !Double.isFinite(value) ? -1 : value.longValue();
    }
    private boolean valid(PluginCall call) { return foreground && active > floor && generation(call) == active; }
    private void event(long generation, String reason) {
        JSObject data = new JSObject(); data.put("generation", generation); data.put("reason", reason);
        notifyListeners("audioStopped", data);
    }
    private void stopRecording() {
        if (recordDeadline != null) { recordDeadline.cancel(false); recordDeadline = null; }
        AudioRecord old = recorder; recorder = null;
        if (old != null) { try { old.stop(); } catch (RuntimeException ignored) {} old.release(); }
    }
    private void stopPlayer(boolean success) {
        if (playDeadline != null) { playDeadline.cancel(false); playDeadline = null; }
        if (player != null) { try { player.pause(); player.flush(); } catch (RuntimeException ignored) {} player.release(); player = null; }
        PluginCall old = playCall; playCall = null;
        if (old != null) { if (success) old.resolve(); else old.reject("Audio stopped"); }
    }
    private void cleanup() {
        stopRecording(); stopPlayer(false);
        if (samples != null) Arrays.fill(samples, (short) 0);
        samples = null; count = 0;
        if (leaseDeadline != null) { leaseDeadline.cancel(false); leaseDeadline = null; }
        if (focus != null) { manager.abandonAudioFocusRequest(focus); focus = null; }
    }
    private void interrupt(String reason) {
        long old = active; floor = Math.max(floor, active); cleanup();
        if (old != 0) event(old, reason);
    }
    private void audioFocus() {
        if (focus != null) return;
        long focusGeneration = active;
        focus = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
            .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setOnAudioFocusChangeListener(change -> {
                if (change < 0) synchronized (BrainoidAudioPlugin.this) { if (active == focusGeneration && focus != null) interrupt("interrupted"); }
            }, main).build();
        if (manager.requestAudioFocus(focus) != AudioManager.AUDIOFOCUS_REQUEST_GRANTED) {
            manager.abandonAudioFocusRequest(focus); focus = null; throw new IllegalStateException();
        }
    }
    @PluginMethod public synchronized void activate(PluginCall call) {
        long gen = generation(call);
        Double expires = number(call, "expiresAt");
        if (!foreground || gen <= Math.max(floor, active) || expires == null || !Double.isFinite(expires) || expires <= System.currentTimeMillis()) { call.reject("Audio context ended"); return; }
        cleanup(); floor = Math.max(floor, active); active = gen;
        leaseDeadline = clock.schedule(() -> { synchronized (this) { if (active == gen) interrupt("interrupted"); } }, (long) (expires - System.currentTimeMillis()), TimeUnit.MILLISECONDS);
        call.resolve();
    }
    @PluginMethod public synchronized void permission(PluginCall call) {
        if (!valid(call)) { call.reject("Audio context ended"); return; }
        if (getPermissionState("microphone") == PermissionState.GRANTED) permissionResult(call);
        else requestPermissionForAlias("microphone", call, "permissionResult");
    }
    @PermissionCallback private synchronized void permissionResult(PluginCall call) {
        if (!valid(call)) { call.reject("Audio context ended"); return; }
        JSObject result = new JSObject(); result.put("granted", getPermissionState("microphone") == PermissionState.GRANTED); call.resolve(result);
    }
    @PluginMethod public synchronized void start(PluginCall call) {
        if (!valid(call) || recorder != null || samples != null || player != null || getPermissionState("microphone") != PermissionState.GRANTED) { call.reject("Recording unavailable"); return; }
        long gen = active;
        try {
            audioFocus();
            int minimum = AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            if (minimum < 1) throw new IllegalStateException();
            recorder = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, 16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(minimum, 8192));
            if (recorder.getState() != AudioRecord.STATE_INITIALIZED) throw new IllegalStateException();
            samples = new short[PcmWav.MAX_FRAMES]; count = 0;
            AudioRecord source = recorder; source.startRecording();
            recordDeadline = clock.schedule(() -> { synchronized (this) { if (active == gen && recorder == source) { stopRecording(); event(gen, "limit"); } } }, 60, TimeUnit.SECONDS);
            new Thread(() -> capture(source, gen), "BrainoidAudioCapture").start(); call.resolve();
        } catch (RuntimeException error) { interrupt("error"); call.reject("Recording unavailable"); }
    }
    private void capture(AudioRecord source, long gen) {
        short[] chunk = new short[2048];
        try {
            while (true) {
                int length = source.read(chunk, 0, chunk.length, AudioRecord.READ_BLOCKING);
                synchronized (this) {
                    if (active != gen || recorder != source || active <= floor) break;
                    if (length < 0) { interrupt("error"); break; }
                    int take = Math.min(length, PcmWav.MAX_FRAMES - count);
                    System.arraycopy(chunk, 0, samples, count, take); count += take;
                    if (count == PcmWav.MAX_FRAMES) { stopRecording(); event(gen, "limit"); break; }
                }
            }
        } catch (RuntimeException error) { synchronized (this) { if (recorder == source) interrupt("error"); } }
        finally { Arrays.fill(chunk, (short) 0); }
    }
    @PluginMethod public synchronized void finish(PluginCall call) {
        if (!valid(call) || samples == null) { call.reject("Recording unavailable"); return; }
        stopRecording();
        try {
            byte[] wav = PcmWav.encode(samples, count);
            JSObject data = new JSObject(); data.put("wav", Base64.encodeToString(wav, Base64.NO_WRAP));
            Arrays.fill(wav, (byte) 0); call.resolve(data);
        } catch (RuntimeException error) { call.reject("Invalid audio"); }
        finally { Arrays.fill(samples, (short) 0); samples = null; count = 0; }
    }
    @PluginMethod public synchronized void play(PluginCall call) {
        String encoded = call.getString("wav"); Double expires = number(call, "expiresAt");
        if (!valid(call) || recorder != null || samples != null || player != null || encoded == null || encoded.length() > ((PcmWav.MAX_BYTES + 2) / 3) * 4 || expires == null || !Double.isFinite(expires) || expires <= System.currentTimeMillis()) { call.reject("Playback unavailable"); return; }
        byte[] wav = null, pcm = null;
        try {
            wav = Base64.decode(encoded, Base64.NO_WRAP); pcm = PcmWav.decode(wav); audioFocus();
            player = new AudioTrack.Builder().setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(16000).setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                .setTransferMode(AudioTrack.MODE_STATIC).setBufferSizeInBytes(pcm.length).build();
            AudioTrack target = player; playCall = call;
            if (target.write(pcm, 0, pcm.length) != pcm.length) throw new IllegalStateException();
            target.setPlaybackPositionUpdateListener(new AudioTrack.OnPlaybackPositionUpdateListener() {
                @Override public void onMarkerReached(AudioTrack track) { synchronized (BrainoidAudioPlugin.this) { if (player == track) stopPlayer(true); } }
                @Override public void onPeriodicNotification(AudioTrack track) {}
            }, main);
            if (target.setNotificationMarkerPosition(pcm.length / 2) != AudioTrack.SUCCESS) throw new IllegalStateException();
            long gen = active;
            playDeadline = clock.schedule(() -> { synchronized (this) { if (active == gen && player == target) interrupt("interrupted"); } }, Math.max(1L, Math.min(61000L, (long) (expires - System.currentTimeMillis()))), TimeUnit.MILLISECONDS);
            target.play();
        } catch (RuntimeException error) { boolean held = playCall == call; interrupt("error"); if (!held) call.reject("Playback unavailable"); }
        finally { if (wav != null) Arrays.fill(wav, (byte) 0); if (pcm != null) Arrays.fill(pcm, (byte) 0); }
    }
    @PluginMethod public synchronized void stop(PluginCall call) {
        long gen = generation(call); floor = Math.max(floor, gen);
        if (active <= floor) cleanup(); call.resolve();
    }
    @Override protected synchronized void handleOnPause() { foreground = false; interrupt("background"); }
    @Override protected synchronized void handleOnResume() { foreground = true; }
    @Override protected synchronized void handleOnDestroy() { foreground = false; interrupt("background"); manager.unregisterAudioDeviceCallback(routes); clock.shutdownNow(); }
}
