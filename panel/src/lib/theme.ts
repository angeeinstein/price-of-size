const KEY = "pos-theme";

export function initialDark(): boolean {
  const saved = localStorage.getItem(KEY);
  if (saved) return saved === "dark";
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function applyTheme(dark: boolean) {
  document.documentElement.classList.toggle("dark", dark);
  localStorage.setItem(KEY, dark ? "dark" : "light");
}
