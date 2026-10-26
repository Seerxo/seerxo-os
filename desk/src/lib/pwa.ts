// Registration is shared by the shell and the System view; the worker uses network-only fetches.
export const serviceWorker = "serviceWorker" in navigator && window.isSecureContext
  ? navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" })
  : null

if (serviceWorker) {
  serviceWorker.then(registration => {
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) void registration.update()
    })
  }).catch(() => { /* System shows the registration error when opened. */ })
}

export const installed = () => window.matchMedia("(display-mode: standalone)").matches ||
  (navigator as Navigator & { standalone?: boolean }).standalone === true
