/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: {
          950: '#0a0a0f',
          900: '#111118',
          800: '#1a1a24',
          700: '#262634',
        },
        accent: '#4f8cff',
      },
    },
  },
  plugins: [],
}
