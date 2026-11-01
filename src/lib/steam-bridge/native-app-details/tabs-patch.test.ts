import { describe, expect, it, vi } from "vitest";

// The ownership gate reaches @decky/api, which only exists inside Steam.
vi.mock("./gate", () => ({ isUnifideckAppId: () => true }));

import { appOverviewOfTabs } from "./tabs-patch";

/** The shape Steam's App-Details tab list has (read live 2026-11-02). */
function appTabs(appid: number) {
  const content = (name: string) => ({
    props: { name, children: { props: { overview: { appid } } } },
  });
  return [
    { id: "WhatsNew", title: "#AppDetails_Tab_Activity", content: content("activity") },
    { id: "YourStuff", title: "#AppDetails_Tab_YourStuff", content: content("achievements") },
    { id: "Community", title: "#AppDetails_Tab_Community", content: content("friends") },
    { id: "GameInfo", title: "#AppDetails_Tab_GameInfo", content: content("info") },
  ];
}

describe("appOverviewOfTabs", () => {
  it("finds the app on an App-Details tab list", () => {
    expect(appOverviewOfTabs(appTabs(3759543982))?.appid).toBe(3759543982);
  });

  it("ignores every other tabbed page in Steam", () => {
    expect(appOverviewOfTabs([{ id: "General", content: {} }])).toBeNull();
    expect(appOverviewOfTabs(undefined)).toBeNull();
    expect(appOverviewOfTabs([{ id: "GameInfo", content: null }])).toBeNull();
  });
});
