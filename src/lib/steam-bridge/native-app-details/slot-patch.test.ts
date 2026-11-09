import { describe, expect, it } from "vitest";
import { patchGetter, patchSlot } from "./slot-patch";

type Render = (x: number) => string;

describe("patchSlot", () => {
  it("wraps and restores", () => {
    const owner: { render: Render } = { render: (x) => `orig ${x}` };
    const original = owner.render;
    const undo = patchSlot<Render>(owner, "render", (orig) => (x) => `ours ${orig(x)}`);
    expect(owner.render(1)).toBe("ours orig 1");
    undo();
    expect(owner.render).toBe(original);
  });

  it("never wraps a wrapper left by a previous plugin instance", () => {
    const owner: { render: Render } = { render: (x) => `orig ${x}` };
    // First instance installs, then the plugin reloads without teardown.
    patchSlot<Render>(owner, "render", (orig) => (x) => `old ${orig(x)}`);
    const undo = patchSlot<Render>(owner, "render", (orig) => (x) => `new ${orig(x)}`);
    expect(owner.render(2)).toBe("new orig 2");
    undo();
    expect(owner.render(2)).toBe("orig 2");
  });

  it("does not clobber a foreign patch layered on top", () => {
    const owner: { render: Render } = { render: (x) => `orig ${x}` };
    const undo = patchSlot<Render>(owner, "render", (orig) => (x) => `ours ${orig(x)}`);
    const ours = owner.render;
    const foreign: Render = (x) => `foreign ${ours(x)}`;
    owner.render = foreign;
    undo();
    expect(owner.render).toBe(foreign);
  });
});

describe("patchGetter", () => {
  class Button {
    calls = 0;
  }
  Object.defineProperty(Button.prototype, "OnClick", {
    configurable: true,
    get(this: Button) {
      const bound = () => {
        this.calls++;
      };
      Object.defineProperty(this, "OnClick", { value: bound });
      return bound;
    },
  });

  it("replaces the getter and restores the original", () => {
    const original = Object.getOwnPropertyDescriptor(Button.prototype, "OnClick");
    const undo = patchGetter(
      Button.prototype,
      "OnClick",
      () =>
        function () {
          return () => "ours";
        },
    );
    expect((new Button() as unknown as { OnClick: () => string }).OnClick()).toBe("ours");
    undo();
    expect(Object.getOwnPropertyDescriptor(Button.prototype, "OnClick")).toEqual(original);
  });

  it("refuses a slot that is not a configurable accessor", () => {
    const proto = { OnClick: () => undefined };
    expect(() => patchGetter(proto, "OnClick", (g) => g)).toThrow();
  });
});
