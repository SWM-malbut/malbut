import { defineConfig } from "drizzle-kit";

export default defineConfig({
  out: "./db/migrations/generated",
  schema: "./db/schema.ts",
  dialect: "postgresql",
});
