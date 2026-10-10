/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./static/index.html", "./static/app.js"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "BlinkMacSystemFont", "Segoe UI", "sans-serif"]
      }
    }
  }
};
