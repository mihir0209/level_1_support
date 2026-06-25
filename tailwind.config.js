/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: ['./static/admin/**/*.html'],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#fdf4ff', 100: '#fae8ff', 200: '#f5d0fe', 300: '#f0abfc',
          400: '#e879f9', 500: '#d946ef', 600: '#c026d3', 700: '#a21caf',
          800: '#86198f', 900: '#701a75',
        },
        surface: {
          50: '#f8fafc', 100: '#f1f5f9', 200: '#e2e8f0', 300: '#cbd5e1',
          700: '#334155', 800: '#1e293b', 900: '#0f172a',
        },
      },
    },
  },
  plugins: [],
};