/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Emit a standalone server bundle for a small production Docker image.
  output: "standalone",
};

export default nextConfig;
