// What the shell's rendered evidence is, as one list both the capture and the
// verification read. A scene is a URL plus whatever has to be opened once the
// page is there; a shot is that scene at one colour scheme and one viewport.

export const VIEWPORTS = Object.freeze({
  desktop: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 },
  // Two device pixels per CSS pixel is enough to read the type in review and
  // keeps the committed file a fraction of a 3x capture.
  mobile: { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true },
});

export const SCHEMES = Object.freeze(["light", "dark"]);

export const SCENES = Object.freeze([
  { name: "empty-project", query: "project=ws_empty", viewports: ["desktop"] },
  { name: "thread-decision", query: "project=ws_mobile&thread=thread_mobile", viewports: ["desktop", "mobile"] },
  { name: "thread-failed", query: "project=ws_mobile&thread=thread_failed", viewports: ["desktop"] },
  { name: "sidebar-archived", query: "project=ws_mobile&thread=thread_mobile", open: "archived", viewports: ["desktop"] },
  { name: "library", query: "project=ws_mobile&view=library", open: "source", viewports: ["desktop"] },
  { name: "inbox", query: "project=ws_mobile&view=inbox", viewports: ["desktop"] },
  { name: "status", query: "project=ws_mobile&view=status", viewports: ["desktop"] },
  { name: "drawer", query: "project=ws_mobile&thread=thread_mobile", open: "drawer", viewports: ["mobile"] },
]);

export function shotName(scene, scheme, viewport) {
  return `${scene}-${scheme}-${viewport}.png`;
}

export function expectedShots() {
  const shots = [];
  for (const scene of SCENES) {
    for (const viewport of scene.viewports) {
      for (const scheme of SCHEMES) {
        shots.push({ scene, scheme, viewport, filename: shotName(scene.name, scheme, viewport) });
      }
    }
  }
  return shots;
}
