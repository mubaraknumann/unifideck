/**
 * The device-code sign-in, as `store_auth("start")` describes it.
 *
 * The backend's `MicrosoftDeviceAuth` returns a pending result whose
 * `metadata` carries `flow: "device_code"` plus the user code. Everything
 * here is an optional hint (see `Result.metadata` on the backend): a result
 * without it is an ordinary browser sign-in.
 */

/** What the frontend needs to know about a running device-code sign-in. */
export interface DeviceCodeStart {
  userCode: string;
  verificationUri: string;
  expiresInSec: number;
}

const DEFAULT_VERIFICATION_URI = "https://www.microsoft.com/link";
const DEFAULT_EXPIRES_IN_SEC = 900;

/** Extra wait past the code's own expiry before the frontend gives up: the
 *  backend's poll owns the deadline and reports it, this is the backstop. */
export const DEVICE_CODE_SLACK_MS = 60 * 1000;

/** The device-code details from a `store_auth("start")` result, or null. */
export function parseDeviceCodeStart(result: unknown): DeviceCodeStart | null {
  if (!result || typeof result !== "object") return null;
  const meta = (result as { metadata?: unknown }).metadata;
  if (!meta || typeof meta !== "object") return null;
  const m = meta as Record<string, unknown>;
  if (m.flow !== "device_code") return null;
  if (typeof m.user_code !== "string" || m.user_code === "") return null;
  return {
    userCode: m.user_code,
    verificationUri:
      typeof m.verification_uri === "string" && m.verification_uri !== ""
        ? m.verification_uri
        : DEFAULT_VERIFICATION_URI,
    expiresInSec:
      typeof m.expires_in === "number" && m.expires_in > 0
        ? m.expires_in
        : DEFAULT_EXPIRES_IN_SEC,
  };
}
