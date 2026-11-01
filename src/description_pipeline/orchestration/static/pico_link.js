export function createLinkSetup(origin, session) {
  if (!["starting", "ready"].includes(session.state) || !/^[0-9a-f]{32}$/.test(session.session_id)) {
    throw new Error("Start an active session before downloading its PICO link setup");
  }
  const url = new URL(origin);
  if (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))) {
    throw new Error("Remote PICO link setup requires an HTTPS portal");
  }
  return {
    schema_version: "solidworks-to-urdf.pico-link/v1",
    origin: `${url.protocol === "https:" ? "wss:" : "ws:"}//${url.host}`,
    session_id: session.session_id,
  };
}

export function downloadLinkSetup(origin, session) {
  const setup = createLinkSetup(origin, session);
  const url = URL.createObjectURL(new Blob([JSON.stringify(setup, null, 2) + "\n"], { type: "application/json" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `pico-link-${setup.session_id}.json`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
