import { Capacitor, registerPlugin } from "@capacitor/core";
import { mobileAudio, type NativeAudioPlugin } from "./audio.ts";

let adapter: ReturnType<typeof mobileAudio> | undefined;

/** No browser microphone fallback; native registration and device release checks are required. */
export function createNativeAudio() {
  if (!Capacitor.isNativePlatform() || !Capacitor.isPluginAvailable("BrainoidAudio"))
    throw new Error("この端末では音声を利用できません。文字で入力できます。");
  adapter ??= mobileAudio(registerPlugin<NativeAudioPlugin>("BrainoidAudio"));
  return adapter;
}
