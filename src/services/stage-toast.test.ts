// @vitest-environment jsdom
/**
 * stage-toast: an achievement payload becomes a Steam-style card (image,
 * name, store logo, "Game · Store"); every other payload stays a text toast.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const { toast } = vi.hoisted(() => ({ toast: vi.fn() }));
vi.mock("@decky/api", () => ({ toaster: { toast } }));
// Steam's React is not loadable under jsdom; a plain element record is
// enough to read the props the toast was given.
vi.mock("react", () => ({
  createElement: (type: unknown, props: unknown) => ({ type, props }),
}));
vi.mock("../components/shared/StoreIcon", () => ({
  StoreIcon: () => null,
}));
vi.mock("./toast-action", () => ({ resolveToastAction: () => undefined }));
// Steam's own icon component lives in Steam's bundle, absent under jsdom;
// a stand-in records the props it would get.
const SteamIcon = vi.hoisted(() => () => null);
vi.mock("../lib/steam-bridge/native-app-details", () => ({
  getAchievementIcon: () => SteamIcon,
}));

import { showStageToast } from "./stage-toast";

describe("showStageToast", () => {
  beforeEach(() => toast.mockClear());

  it("draws a card: game in the header beside the store logo, name below, Steam's icon", () => {
    showStageToast({
      i18n_key: "toasts.achievementName",
      i18n_title_key: "toasts.header.achievement",
      game_title: "9 Kings (Game Preview)",
      achievement: {
        name: "Chainsaw Go Brrrrrrrrr",
        icon: "https://images-eds-ssl.xboxlive.com/image?url=abc",
        store: "microsoft",
        rarity: 9.54,
      },
    });
    expect(toast).toHaveBeenCalledTimes(1);
    const data = toast.mock.calls[0][0];
    // Names from the store are not cut to the 16-character text budget.
    expect(data.title).toBe("9 Kings (Game Preview)");
    expect(data.body).toBe("Chainsaw Go Brrrrrrrrr");
    // Steam's icon component draws the image; its rare rule gets the share.
    expect(data.logo.type).toBe(SteamIcon);
    expect(data.logo.props.globalUnlockPercentage).toBe(9.54);
    expect(data.logo.props.unlocked).toBe(true);
    expect(data.logo.props.achievement.icon_achieved).toBe(
      "https://images-eds-ssl.xboxlive.com/image?url=abc",
    );
    expect(data.icon).toBeTruthy();
  });

  it("falls back to the store logo when there is no image", () => {
    showStageToast({
      game_title: "BioShock 2",
      achievement: { name: "Daddy's Home", icon: "", store: "gog" },
    });
    const data = toast.mock.calls[0][0];
    expect(data.title).toBe("BioShock 2");
    expect(data.body).toBe("Daddy's Home");
    expect(data.logo.props.store).toBe("gog");
  });

  it("names the store in the header only when there is no game title", () => {
    showStageToast({
      game_title: "",
      achievement: { name: "Quartermaster", icon: "", store: "microsoft" },
    });
    expect(toast.mock.calls[0][0].title).toBe("Xbox");
  });

  it("keeps a plain toast for everything else", () => {
    showStageToast({
      i18n_key: "toasts.achievementName",
      i18n_title_key: "toasts.header.achievement",
    });
    const data = toast.mock.calls[0][0];
    expect(data.logo).toBeUndefined();
  });
});
