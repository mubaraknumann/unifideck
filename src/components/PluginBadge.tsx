/**
 * PluginBadge — the "an update is waiting" dot on Unifideck's QAM entry.
 *
 * Decky renders each plugin's row in the Quick Access plugin list as
 * `[icon, name, NotificationBadge]`, and both that `icon` and the
 * `titleView` shown once the plugin is open come straight from our own
 * `definePlugin` return in `index.tsx`. So badging them needs no Decky
 * internals, no patching of its minified bundle, and nothing that breaks
 * when Decky updates — we just hand it components instead of static
 * nodes.
 *
 * Decky's OWN badge was the obvious route and is deliberately NOT used.
 * It is driven by `deckyState._updates.has(name)`, but that same Map
 * feeds Decky's Settings → Plugins page, which builds
 * `requestMultiplePluginInstalls([...updates.entries()])` and a per-row
 * "Update" action pointed at the Decky store CDN. Unifideck self-updates
 * from GitHub and is not a store plugin, so a synthetic entry there would
 * offer the user an install against a hash that does not exist — and be
 * wiped on Decky's next store check anyway.
 *
 * State comes from the module-level store in `lib/plugin-update.ts`
 * rather than an RPC here: the plugin-list row mounts and unmounts every
 * time the user enters or leaves the Decky tab, so a fetch-on-mount would
 * flash an un-badged icon each time.
 */
import { FC, useSyncExternalStore } from "react";
import { FaGamepad } from "react-icons/fa";
import { getUpdatePending, subscribeUpdatePending } from "../lib/plugin-update";

/** Subscribe to the pending-update store in `lib/plugin-update.ts`.
 *  The hook lives here, not there, so that module stays free of any
 *  React import and remains unit-testable (React is peer-provided by
 *  the Steam webview and absent from node_modules). */
const useUpdatePending = (): boolean =>
  useSyncExternalStore(
    subscribeUpdatePending,
    getUpdatePending,
    getUpdatePending,
  );

/** Decorative only — the panel button and the toast carry the wording,
 *  so this is aria-hidden rather than inventing a label to translate. */
const UpdateDot: FC = () => (
  <div
    aria-hidden="true"
    style={{
      position: "absolute",
      top: -2,
      insetInlineEnd: -3,
      width: 8,
      height: 8,
      borderRadius: "50%",
      background: "#58be5b",
      boxShadow: "0 0 0 2px rgba(0, 0, 0, 0.55)",
      pointerEvents: "none",
    }}
  />
);

/** The plugin's QAM list icon, badged when an update is pending. */
export const PluginIcon: FC = () => {
  const pending = useUpdatePending();
  return (
    <span style={{ position: "relative", display: "inline-flex" }}>
      <FaGamepad />
      {pending && <UpdateDot />}
    </span>
  );
};

/** The plugin's QAM header, badged when an update is pending. */
export const PluginTitleView: FC = () => {
  const pending = useUpdatePending();
  return (
    <div style={{ position: "relative", display: "inline-block" }}>
      Unifideck
      {pending && <UpdateDot />}
    </div>
  );
};
