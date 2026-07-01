export function initTheme() {
  const saved = localStorage.getItem("beacon_theme");
  const dark = saved ? saved === "dark" : true;
  document.documentElement.classList.toggle("dark", dark);
  return dark;
}
export function toggleTheme() {
  const dark = !document.documentElement.classList.contains("dark");
  document.documentElement.classList.toggle("dark", dark);
  localStorage.setItem("beacon_theme", dark ? "dark" : "light");
  return dark;
}
