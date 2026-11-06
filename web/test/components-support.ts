// Stand-ins for browser layout that happy-dom does not perform.
import { act } from "@testing-library/react";

/** A boolean keeps a failing assertion from printing a whole detached DOM node. */
export function isPresent(element: Element | null): boolean {
  return element !== null;
}

/** A ResizeObserver the test drives: happy-dom's observer never reports sizes. */
class ManualResizeObserver {
  readonly targets: Element[] = [];
  disconnected = false;
  private readonly callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
  }

  observe(target: Element): void {
    this.targets.push(target);
  }

  unobserve(target: Element): void {
    const index = this.targets.indexOf(target);
    if (index >= 0) this.targets.splice(index, 1);
  }

  disconnect(): void {
    this.disconnected = true;
    this.targets.length = 0;
  }

  /** Report the observed element's content width, as a layout change would. */
  resize(width: number): void {
    const target = this.targets[0];
    if (!target) throw new Error("the observer has no target to resize");
    act(() => {
      this.callback(
        [
          {
            target,
            contentRect: new DOMRectReadOnly(0, 0, width, 0),
            borderBoxSize: [],
            contentBoxSize: [],
            devicePixelContentBoxSize: [],
          },
        ],
        this,
      );
    });
  }
}

export interface ResizeObserverStub {
  /** Every observer constructed since the stub was installed, oldest first. */
  readonly observers: ManualResizeObserver[];
  latest(): ManualResizeObserver;
  restore(): void;
}

/** Replace the global ResizeObserver until `restore` runs; call it from afterEach. */
export function stubResizeObserver(): ResizeObserverStub {
  const original = globalThis.ResizeObserver;
  const observers: ManualResizeObserver[] = [];
  globalThis.ResizeObserver = class extends ManualResizeObserver {
    constructor(callback: ResizeObserverCallback) {
      super(callback);
      observers.push(this);
    }
  };
  return {
    observers,
    latest() {
      const observer = observers.at(-1);
      if (!observer) throw new Error("no ResizeObserver was constructed");
      return observer;
    },
    restore() {
      globalThis.ResizeObserver = original;
    },
  };
}
