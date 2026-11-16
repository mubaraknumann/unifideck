/**
 * LunaQualitySection — Amazon Luna streaming-quality picker.
 *
 * Luna keeps its quality in the browser only (localStorage on the Luna
 * site), so the choice is stored in the plugin config and written into the
 * Luna kiosk on the next launch (`stores/luna/luna_video_quality.py`).
 * "Auto" leaves whatever Luna has stored. Shown only while Luna is
 * connected, so it does not clutter the panel for everyone else.
 */
import { FC, useEffect, useMemo, useState } from "react";
import { Dropdown, PanelSection, PanelSectionRow } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { rpcRoutes } from "../../api/rpc-routes";
import { useRPCMutation, useRPCQuery } from "../../api/useRPC";
import { useStoreAuth } from "../../hooks/useStoreAuth";

/** Luna's own quality levels, plus "" for Auto. */
export type LunaVideoQuality = "" | "LIMITED" | "STANDARD" | "BEST";

interface QualityPayload {
  quality: LunaVideoQuality;
}

export const LunaQualitySection: FC = () => {
  const { t } = useTranslation();
  const luna = useStoreAuth("luna");
  const connected = luna.status === "connected";
  const query = useRPCQuery<[], QualityPayload>(
    rpcRoutes.getLunaVideoQuality,
    [],
    { enabled: connected },
  );
  const save = useRPCMutation<[LunaVideoQuality], QualityPayload>(
    rpcRoutes.setLunaVideoQuality,
  );
  const [quality, setQuality] = useState<LunaVideoQuality>("");

  useEffect(() => {
    if (query.data) setQuality(query.data.quality ?? "");
  }, [query.data]);

  const options = useMemo(
    () => [
      { data: "", label: t("settings.lunaQuality.auto") },
      { data: "LIMITED", label: t("settings.lunaQuality.limited") },
      { data: "STANDARD", label: t("settings.lunaQuality.standard") },
      { data: "BEST", label: t("settings.lunaQuality.best") },
    ],
    [t],
  );

  if (!connected) return null;

  return (
    <PanelSection title={t("settings.lunaQuality.title")}>
      <PanelSectionRow>
        <Dropdown
          rgOptions={options}
          selectedOption={quality}
          onChange={(opt) => {
            const next = String(opt.data) as LunaVideoQuality;
            setQuality(next);
            void save.mutate(next);
          }}
        />
      </PanelSectionRow>
    </PanelSection>
  );
};
