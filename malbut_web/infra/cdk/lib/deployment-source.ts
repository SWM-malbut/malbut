import * as fs from "node:fs";
import * as path from "node:path";
import { IgnoreMode, SymlinkFollowMode } from "aws-cdk-lib";
import { Asset } from "aws-cdk-lib/aws-s3-assets";
import { Construct } from "constructs";

// Build artifacts, local credentials and CDK itself must never enter the image
// source asset. The image is built from the selected working-tree snapshot,
// independently of whether those changes have been committed or pushed.
// The Lambda sources under infra/aws stay in: the web imports the push
// broker's notification module at build time.
export const DEPLOYMENT_SOURCE_EXCLUDES = [
  ".git", "**/.git", ".github", "**/node_modules", "node_modules",
  ".next", ".vinext", ".wrangler", "coverage", "dist", "artifacts", "*.tsbuildinfo",
  ".env", ".env.*", "**/.env", "**/.env.*", ".local", "**/.local",
  "infra/cdk", "npm-debug.log*", "design-qa.md",
];

export function resolveDeploymentSource(
  directory: string,
  repositoryRoot = findRepositoryRoot(__dirname),
): string {
  if (!directory || path.isAbsolute(directory) || directory.split(/[\\/]/).some(
    (part) => !part || part === "." || part === "..",
  )) {
    throw new Error("deploymentSourceDirectory must be a repository-relative directory without traversal");
  }
  const root = fs.realpathSync(repositoryRoot);
  const candidate = fs.realpathSync(path.resolve(root, directory));
  if (!candidate.startsWith(`${root}${path.sep}`) || !fs.statSync(candidate).isDirectory()) {
    throw new Error("deploymentSourceDirectory must stay inside the repository");
  }
  for (const required of ["Dockerfile", "package.json", "package-lock.json", "next.config.ts"]) {
    const filename = path.join(candidate, required);
    if (!fs.lstatSync(filename).isFile()) {
      throw new Error(`deploymentSourceDirectory requires a regular ${required}`);
    }
  }
  return candidate;
}

export function createDeploymentSource(scope: Construct, directory: string | undefined) {
  if (directory === undefined) return undefined;
  return new Asset(scope, "HomecamDeploymentSource", {
    path: resolveDeploymentSource(directory),
    exclude: DEPLOYMENT_SOURCE_EXCLUDES,
    ignoreMode: IgnoreMode.GLOB,
    followSymlinks: SymlinkFollowMode.NEVER,
  });
}

function findRepositoryRoot(start: string): string {
  let directory = start;
  while (!fs.existsSync(path.join(directory, ".git"))) {
    const parent = path.dirname(directory);
    if (parent === directory) throw new Error("Cannot find repository root for deployment snapshot");
    directory = parent;
  }
  return directory;
}
