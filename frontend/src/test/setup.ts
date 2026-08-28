import "@testing-library/jest-dom/vitest";

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

Object.defineProperty(globalThis, "ResizeObserver", {
  value: ResizeObserverMock,
  writable: true,
});

Object.defineProperty(globalThis.HTMLElement.prototype, "hasPointerCapture", {
  value: () => false,
});

Object.defineProperty(globalThis.HTMLElement.prototype, "setPointerCapture", {
  value: () => undefined,
});

Object.defineProperty(globalThis.HTMLElement.prototype, "releasePointerCapture", {
  value: () => undefined,
});
