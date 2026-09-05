/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#F7F8FA",
        surface: "#FFFFFF",
        "surface-raised": "#FFFFFF",
        "surface-hover": "#F1F3F7",
        border: "#E4E7EC",
        "border-strong": "#D0D5DD",
        text: "#101828",
        "text-muted": "#667085",
        "text-faint": "#98A2B3",
        accent: "#4F46E5",
        "accent-bg": "#EEF0FF",
        success: "#12946F",
        "success-bg": "#E8F5F0",
        warning: "#B54708",
        "warning-bg": "#FDF1E8",
        danger: "#C0152F",
        "danger-bg": "#FCEAEC",
      },
      fontFamily: {
        sans: ["Inter", "sans-serif"],
        mono: ["JetBrains Mono", "monospace"],
      },
      boxShadow: {
        panel: "0 1px 2px rgba(16,24,40,0.06), 0 1px 3px rgba(16,24,40,0.08)",
      },
    },
  },
  plugins: [],
};
