"use client";

import { useEffect, useState } from "react";

export type ConnectivityState = "online" | "offline" | "reconnected";

export function PwaRegistration() {
  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;

    void navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {
      // The fixture shell remains usable when service workers are unavailable.
    });
  }, []);

  return null;
}

export function useConnectivityState(): ConnectivityState {
  const [state, setState] = useState<ConnectivityState>("online");

  useEffect(() => {
    const handleOffline = () => setState("offline");
    const handleOnline = () =>
      setState((current) => (current === "offline" ? "reconnected" : "online"));

    if (!navigator.onLine) handleOffline();
    window.addEventListener("offline", handleOffline);
    window.addEventListener("online", handleOnline);
    return () => {
      window.removeEventListener("offline", handleOffline);
      window.removeEventListener("online", handleOnline);
    };
  }, []);

  return state;
}

export function ConnectionBanner({ state, mode = "demo" }: { state: ConnectivityState; mode?: "demo" | "control" }) {
  if (state === "online") return null;

  const offlineTitle = mode === "demo" ? "Offline · read-only fixture" : "Offline · durable state is read-only";
  const offlineCopy = mode === "demo"
    ? "Current fixture content remains visible, but actions are unavailable and nothing is persisted."
    : "The last validated screen remains visible. No command is queued or stored in this browser.";
  const restoredCopy = mode === "demo"
    ? "The canonical fixture shell is available again. No offline action was queued."
    : "Cortex is replaying durable events from the saved opaque cursor. No offline action was queued.";

  return (
    <div
      aria-live="polite"
      className={`connection-banner ${state}`}
      data-connection-state={state}
      role="status"
    >
      <strong>{state === "offline" ? offlineTitle : "Connection restored"}</strong>
      <span>
        {state === "offline" ? offlineCopy : restoredCopy}
      </span>
    </div>
  );
}
