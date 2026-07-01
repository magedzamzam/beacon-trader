/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)", panel: "var(--panel)", panel2: "var(--panel-2)",
        edge: "var(--edge)", ink: "var(--ink)", muted: "var(--muted)",
        beacon: "var(--beacon)", long: "var(--long)", short: "var(--short)",
        warn: "var(--warn)",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["'JetBrains Mono'", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      boxShadow: { panel: "0 1px 0 0 var(--edge), 0 8px 24px -12px rgba(0,0,0,.5)" },
    },
  },
  plugins: [],
};
