import type { Device, DevicePatch, PairingConfirm } from "@brainoid-internal/api-client";
import { useEffect, useState, useSyncExternalStore } from "react";
import type { SessionContext } from "../session.ts";
import { pairing, patch, scopeNames, scopes, statusNames } from "./contracts.ts";
import { DevicesController } from "./controller.ts";

export function Devices({
  context,
  surface,
}: {
  context: SessionContext;
  surface: "mobile" | "display";
}) {
  const [controller] = useState(() => new DevicesController(context, surface));
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  useEffect(() => {
    void controller.load();
    return () => controller.dispose();
  }, [controller]);
  if (surface === "display")
    return (
      <section className="card" aria-label="Piの管理">
        <h2>Piの登録・管理</h2>
        <p>
          登録コードはこのPiの登録画面で表示し、オーナーまたは管理者のスマホから承認してください。
        </p>
      </section>
    );
  const manager = state.tenant?.my_role === "owner" || state.tenant?.my_role === "admin";
  const disabled = state.busy || state.pending;
  return (
    <section className="card device-workspace" aria-label="Piの管理">
      <h2>Piの登録・管理</h2>
      {state.notice && <p role="alert">{state.notice}</p>}
      {state.busy && <p role="status">端末の情報を確認しています。</p>}
      {state.closed ? (
        <p>所属を確認し直してください。</p>
      ) : (
        <>
          {(!state.tenant || manager) && (
            <button
              className="button secondary"
              type="button"
              disabled={disabled}
              onClick={() => void controller.load()}
            >
              端末一覧を再取得
            </button>
          )}
          {manager && (
            <>
              <p>管理するテナント: {state.tenant?.name}</p>
              {state.pending && (
                <p>
                  前回と同じ操作の結果を確認します。現在の権限:{" "}
                  {state.tenant?.my_role === "owner" ? "オーナー" : "管理者"}。登録先ID:{" "}
                  {state.tenant?.id}
                </p>
              )}
              <button
                className="button primary"
                type="button"
                disabled={disabled}
                onClick={() => void controller.beginPair()}
              >
                Piを登録
              </button>
              {!state.busy && state.devices.length === 0 && <p>登録された端末はありません。</p>}
              <ul className="tenant-list">
                {state.devices.map((v) => (
                  <li key={v.id}>
                    <button
                      type="button"
                      className="button secondary"
                      disabled={disabled}
                      onClick={() => void controller.select(v.id)}
                    >
                      {v.name} · {statusNames[v.status]}
                    </button>
                  </li>
                ))}
              </ul>
              {state.selected && (
                <section aria-label="端末の詳細">
                  <DeviceCard value={state.selected} />
                  {!state.conflict && state.selected.status !== "revoked" && (
                    <>
                      <button
                        type="button"
                        className="button secondary"
                        disabled={disabled || state.selected.status === "suspended"}
                        onClick={() => controller.edit()}
                      >
                        許可設定を編集
                      </button>
                      <Revoke
                        key={`${state.selected.id}:${state.selected.version}`}
                        disabled={disabled}
                        revoke={() => void controller.revoke(true)}
                      />
                    </>
                  )}
                  {state.selected.status === "revoked" && (
                    <button
                      type="button"
                      className="button primary"
                      disabled={disabled}
                      onClick={() => void controller.beginPair(state.selected?.id)}
                    >
                      このPiを再登録
                    </button>
                  )}
                </section>
              )}
              {state.revokedId && !state.pairing && (
                <button
                  type="button"
                  className="button secondary"
                  disabled={disabled}
                  onClick={() => void controller.beginPair(state.revokedId)}
                >
                  失効したPiを再登録
                </button>
              )}
              {state.conflict && (
                <section aria-label="端末設定の競合">
                  <h3>変更前と現在の内容</h3>
                  <p>変更前</p>
                  <DeviceCard value={state.conflict.before} />
                  {state.conflict.current && (
                    <>
                      <p>現在</p>
                      <DeviceCard value={state.conflict.current} />
                      <button
                        type="button"
                        className="button primary"
                        disabled={disabled}
                        onClick={() => controller.acceptCurrent()}
                      >
                        現在の設定を確認した
                      </button>
                    </>
                  )}
                </section>
              )}
              {state.pairing && !state.review && (
                <PairForm
                  key={state.pairing.replaces_device_id ?? "new"}
                  body={state.pairing}
                  disabled={disabled}
                  change={(v) => controller.draftPair(v)}
                  submit={() => void controller.reviewPair()}
                />
              )}
              {state.review && (
                <PairReview
                  key={JSON.stringify(state.review)}
                  body={state.review.body}
                  tenant={state.review.tenant.name}
                  tenantId={state.review.tenant.id}
                  disabled={disabled}
                  submit={() => void controller.confirmPair(state.review?.tenant.id ?? "", true)}
                />
              )}
              {state.editing && (
                <EditForm
                  key={`${state.selected?.id}:${state.selected?.version}`}
                  body={state.editing}
                  disabled={disabled}
                  change={(v) => controller.draftEdit(v)}
                  submit={() => void controller.save()}
                />
              )}
              {(state.pairing || state.editing || state.review) && (
                <button
                  type="button"
                  className="button secondary"
                  disabled={disabled}
                  onClick={() => controller.cancel()}
                >
                  取り消す
                </button>
              )}
              {state.pending && (
                <button
                  type="button"
                  className="button primary"
                  disabled={state.busy}
                  onClick={() => void controller.retry()}
                >
                  同じ操作の受付を再確認
                </button>
              )}
              <p>
                クラウドでの失効とPi内の秘密の消去は別です。オフラインのPiの消去は確認できません。譲渡前はPi側の初期化と外部サービスの資格情報失効も確認してください。
              </p>
            </>
          )}
        </>
      )}
    </section>
  );
}
export function DeviceCard({ value }: { value: Device }) {
  return (
    <div className="device-card">
      <h3>{value.name}</h3>
      <dl>
        <dt>状態</dt>
        <dd>{statusNames[value.status]}</dd>
        <dt>端末ID</dt>
        <dd>{value.id}</dd>
        <dt>能力（申告値）</dt>
        <dd>{value.capabilities.join("、") || "未登録"}</dd>
        <dt>許可する機能</dt>
        <dd>{value.allowed_scopes?.map((v) => scopeNames[v]).join("、") || "なし"}</dd>
        <dt>許可する対象ID</dt>
        <dd>{value.allowed_entity_ids.join("、") || "なし"}</dd>
        <dt>許可する部屋ID</dt>
        <dd>{value.allowed_room_ids?.join("、") || "なし"}</dd>
      </dl>
      <p>能力や許可設定は機器の接続・到達性を保証しません。</p>
      {value.status === "revoked" && <p>クラウド: 失効済み / Pi内の秘密の消去: 未確認</p>}
    </div>
  );
}
const parseIds = (value: string) => (value.trim() ? value.trim().split(/[\s,、]+/) : []);
function PairForm({
  body,
  disabled,
  change,
  submit,
}: {
  body: PairingConfirm;
  disabled: boolean;
  change: (body: PairingConfirm) => void;
  submit: () => void;
}) {
  const [targets, setTargets] = useState(body.allowed_entity_ids.join("\n"));
  return (
    <form
      aria-label="Piの登録内容"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <h3>{body.replaces_device_id ? "失効したPiの再登録" : "登録するPi"}</h3>
      <p>
        Piに表示された8文字のコードを入力してください。有効期限はPiで確認できます（発行から5分）。期限切れ・使用済みの場合は新しいコードが必要です。
      </p>
      {body.replaces_device_id && (
        <p>
          置き換える端末ID: {body.replaces_device_id}
          。同じPi・同じテナントで再登録し、以前の許可機能と部屋の設定を引き継ぎます。
        </p>
      )}
      <label>
        登録コード
        <input
          autoComplete="off"
          autoCapitalize="characters"
          maxLength={8}
          required
          value={body.user_code}
          disabled={disabled}
          onChange={(event) => change({ ...body, user_code: event.target.value.toUpperCase() })}
        />
      </label>
      <label>
        端末名
        <input
          maxLength={200}
          required
          value={body.device_name}
          disabled={disabled}
          onChange={(event) => change({ ...body, device_name: event.target.value })}
        />
      </label>
      <label>
        許可する対象ID（1行に1件、空欄は許可なし）
        <textarea
          value={targets}
          disabled={disabled}
          onChange={(event) => {
            setTargets(event.target.value);
            change({ ...body, allowed_entity_ids: parseIds(event.target.value) });
          }}
        />
      </label>
      <p>対象はこのテナントに登録されたIDを指定してください。登録時に所属と有効性を確認します。</p>
      <button type="submit" className="button primary" disabled={disabled || !pairing(body)}>
        登録内容を確認
      </button>
    </form>
  );
}
function PairReview({
  body,
  tenant,
  tenantId,
  disabled,
  submit,
}: {
  body: PairingConfirm;
  tenant: string;
  tenantId: string;
  disabled: boolean;
  submit: () => void;
}) {
  const [checked, setChecked] = useState(false);
  return (
    <section aria-label="Piの登録確認">
      <h3>登録先と許可対象の確認</h3>
      <p>登録先: {tenant}</p>
      <p>テナントID: {tenantId}</p>
      <p>端末名: {body.device_name}</p>
      <p>コード: {body.user_code}</p>
      <p>許可する対象ID: {body.allowed_entity_ids.join("、") || "なし"}</p>
      {body.replaces_device_id && <p>失効した端末ID: {body.replaces_device_id}</p>}
      <p>
        Piの画面とこの内容を照合してください。登録先は後から別のテナントへ移せません。登録後に機能と部屋の許可設定を確認してください。
      </p>
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(e) => setChecked(e.target.checked)}
        />
        登録先「{tenant}」と端末・許可対象を確認しました
      </label>
      <button
        type="button"
        className="button primary"
        disabled={disabled || !checked}
        onClick={submit}
      >
        このテナントへ登録を承認
      </button>
    </section>
  );
}
function EditForm({
  body,
  disabled,
  change,
  submit,
}: {
  body: DevicePatch;
  disabled: boolean;
  change: (body: DevicePatch) => void;
  submit: () => void;
}) {
  const [entities, setEntities] = useState(body.allowed_entity_ids?.join("\n") ?? ""),
    [rooms, setRooms] = useState(body.allowed_room_ids?.join("\n") ?? "");
  return (
    <form
      aria-label="Piの許可設定"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <h3>許可設定</h3>
      <label>
        端末名
        <input
          value={body.name ?? ""}
          maxLength={200}
          required
          disabled={disabled}
          onChange={(e) => change({ ...body, name: e.target.value })}
        />
      </label>
      <fieldset disabled={disabled}>
        <legend>許可する機能</legend>
        {scopes.map((scope) => (
          <label className="checkbox-label" key={scope}>
            <input
              type="checkbox"
              checked={body.allowed_scopes?.includes(scope) ?? false}
              onChange={(e) =>
                change({
                  ...body,
                  allowed_scopes: e.target.checked
                    ? [...(body.allowed_scopes ?? []), scope]
                    : (body.allowed_scopes?.filter((s) => s !== scope) ?? []),
                })
              }
            />
            {scopeNames[scope]}
          </label>
        ))}
      </fieldset>
      <label>
        許可する対象ID（1行に1件）
        <textarea
          value={entities}
          disabled={disabled}
          onChange={(e) => {
            setEntities(e.target.value);
            change({ ...body, allowed_entity_ids: parseIds(e.target.value) });
          }}
        />
      </label>
      <label>
        許可する部屋ID（1行に1件）
        <textarea
          value={rooms}
          disabled={disabled}
          onChange={(e) => {
            setRooms(e.target.value);
            change({ ...body, allowed_room_ids: parseIds(e.target.value) });
          }}
        />
      </label>
      <p>
        空欄は許可なしです。同じテナントの有効な対象・部屋のみ保存できます。許可範囲の変更後は接続が停止するため、失効して再登録してください。
      </p>
      <button type="submit" className="button primary" disabled={disabled || !patch(body)}>
        この許可設定を保存
      </button>
    </form>
  );
}
function Revoke({ disabled, revoke }: { disabled: boolean; revoke: () => void }) {
  const [checked, setChecked] = useState(false);
  return (
    <div>
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(e) => setChecked(e.target.checked)}
        />
        この端末のクラウド利用を停止することを確認しました
      </label>
      <button
        type="button"
        className="button secondary"
        disabled={disabled || !checked}
        onClick={revoke}
      >
        端末を失効
      </button>
    </div>
  );
}
