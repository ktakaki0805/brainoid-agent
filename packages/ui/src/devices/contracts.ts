import type {
  Device,
  DeviceOperationScope,
  DevicePatch,
  PairingConfirm,
} from "@brainoid-internal/api-client";
import { isId } from "../chat/events.ts";
import { validTimestamp } from "../tasks/controller.ts";

export const scopes: readonly DeviceOperationScope[] = [
  "shared",
  "conversation",
  "memory.read",
  "memory.write",
  "notes",
  "tasks",
  "reminders",
  "calendar.read",
  "calendar.propose",
  "home.control",
];
export const scopeNames: Record<DeviceOperationScope, string> = {
  shared: "共有",
  conversation: "会話",
  "memory.read": "記憶の参照",
  "memory.write": "記憶の保存",
  notes: "ノート",
  tasks: "タスク",
  reminders: "リマインダー",
  "calendar.read": "予定の参照",
  "calendar.propose": "予定の提案",
  "home.control": "家電の操作",
};
export const statusNames: Record<Device["status"], string> = {
  active: "登録済み",
  offline: "オフライン",
  suspended: "利用停止中",
  revoked: "失効済み",
};
export const validName = (v: unknown): v is string =>
  typeof v === "string" && v.trim().length > 0 && [...v].length <= 200;
export const ids = (v: unknown): v is string[] =>
  Array.isArray(v) && v.length <= 100 && v.every(isId) && new Set(v).size === v.length;
const validScopes = (v: unknown): v is DeviceOperationScope[] =>
  Array.isArray(v) &&
  v.length <= 10 &&
  v.every((s) => scopes.includes(s)) &&
  new Set(v).size === v.length;
export function pairing(v: PairingConfirm): boolean {
  return (
    !!v &&
    Object.keys(v).every((k) =>
      ["user_code", "device_name", "allowed_entity_ids", "replaces_device_id"].includes(k),
    ) &&
    /^[A-Z0-9]{8}$/.test(v.user_code) &&
    validName(v.device_name) &&
    ids(v.allowed_entity_ids) &&
    (v.replaces_device_id === undefined || isId(v.replaces_device_id))
  );
}
export function patch(v: DevicePatch): boolean {
  return (
    !!v &&
    Object.keys(v).length > 0 &&
    Object.entries(v).every(([key, value]) =>
      key === "name"
        ? validName(value)
        : key === "allowed_scopes"
          ? validScopes(value)
          : ["allowed_entity_ids", "allowed_room_ids"].includes(key) && ids(value),
    )
  );
}
/** The actual API requires fields marked optional in the generated legacy schema. */
export function device(v: Device, tenant: string, id?: string): Device {
  if (
    !v ||
    !isId(v.id) ||
    (id !== undefined && v.id !== id) ||
    v.tenant_id !== tenant ||
    !validName(v.name) ||
    !["active", "revoked", "offline", "suspended"].includes(v.status) ||
    ![v.version, v.authorization_version, v.connection_generation].every(
      (n) => Number.isSafeInteger(n) && Number(n) > 0,
    ) ||
    !validTimestamp(v.created_at) ||
    !validTimestamp(v.updated_at) ||
    !Array.isArray(v.capabilities) ||
    !v.capabilities.every((s) => typeof s === "string" && s.length > 0 && s.length <= 200) ||
    !validScopes(v.allowed_scopes) ||
    !ids(v.allowed_entity_ids) ||
    !ids(v.allowed_room_ids) ||
    Object.keys(v).some(
      (k) =>
        ![
          "id",
          "tenant_id",
          "version",
          "created_at",
          "updated_at",
          "name",
          "status",
          "capabilities",
          "allowed_entity_ids",
          "connection_generation",
          "authorization_version",
          "allowed_scopes",
          "allowed_room_ids",
        ].includes(k),
    )
  )
    throw new Error("Invalid device response");
  return structuredClone(v);
}
export function freeze<T>(value: T): T {
  if (value && typeof value === "object") {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}
