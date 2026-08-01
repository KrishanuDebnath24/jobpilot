import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      boxShadow: {
        // Soft card elevation used across panels — subtler than Tailwind's default "sm".
        card: "0 1px 2px 0 rgb(15 23 42 / 0.04), 0 1px 4px -1px rgb(15 23 42 / 0.06)",
        "card-hover":
          "0 4px 8px -2px rgb(15 23 42 / 0.08), 0 2px 4px -2px rgb(15 23 42 / 0.05)",
      },
    },
  },
  plugins: [],
};

export default config;
