// Theme: "system" follows the OS; "light"/"dark" are the founder's explicit choice, kept in
// localStorage (per browser). Storage can be unavailable (private mode): fall back to system.
import { useEffect, useState } from "react"

export type Theme = "system" | "light" | "dark"
const KEY = "founder-desk-theme"
const media = matchMedia("(prefers-color-scheme: light)")

export function storedTheme(): Theme {
  try {
    const v = localStorage.getItem(KEY)
    return v === "light" || v === "dark" ? v : "system"
  } catch { return "system" }
}

export const resolved = (t: Theme): "light" | "dark" => (t === "system" ? (media.matches ? "light" : "dark") : t)

export function applyTheme(t: Theme) {
  const mode = resolved(t)
  document.documentElement.classList.toggle("dark", mode === "dark")
  document.documentElement.style.colorScheme = mode
  const color = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
  if (color) color.content = mode === "dark" ? "#11151b" : "#f8f9fb"
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(storedTheme)
  const [, rerender] = useState(0)
  useEffect(() => {
    applyTheme(theme)
    if (theme !== "system") return
    const onChange = () => { applyTheme("system"); rerender(n => n + 1) }
    media.addEventListener("change", onChange)
    return () => media.removeEventListener("change", onChange)
  }, [theme])
  const setTheme = (t: Theme) => {
    try { if (t === "system") localStorage.removeItem(KEY); else localStorage.setItem(KEY, t) } catch { /* not persisted */ }
    setThemeState(t)
  }
  return { theme, setTheme, resolved: resolved(theme) }
}
