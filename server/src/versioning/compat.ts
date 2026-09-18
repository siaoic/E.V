/**
 * 版本兼容比较：主程序声明的 WebUI 版本 vs 实际 WebUI 版本。
 *
 * 语义与 src/webui/version_compatibility.py 逐字对齐：
 * release 按点分段补零比较；pre/dev/post 的 phase 排序为
 * dev(0) < a(1) < b(2) < rc(3) < 正式(4) < post(5)。
 */

export type CompatibilityStatus = "compatible" | "webui_outdated" | "main_program_outdated";

const VERSION_PATTERN =
  /^v?(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?(?:\.?dev(\d+))?(?:\.?post(\d+))?(?:\+[0-9a-z.-]+)?$/i;

interface ParsedVersion {
  release: number[];
  phase: [number, number, number, number];
}

export function parseVersion(version: string): ParsedVersion {
  const match = VERSION_PATTERN.exec(version.trim());
  if (match === null) {
    throw new Error(`不支持的版本号格式: ${version}`);
  }
  const release = match[1].split(".").map((part) => Number.parseInt(part, 10));
  const pre = match[2]?.toLowerCase();
  const preNumber = Number.parseInt(match[3] ?? "0", 10);
  const devNumber = match[4];
  const postNumber = match[5];

  let phase: [number, number, number, number];
  if (pre !== undefined) {
    const phaseRank = pre === "a" ? 1 : pre === "b" ? 2 : 3;
    phase = [phaseRank, preNumber, devNumber !== undefined ? 0 : 1, devNumber !== undefined ? Number.parseInt(devNumber, 10) : 0];
  } else if (devNumber !== undefined) {
    phase = [0, Number.parseInt(devNumber, 10), 0, 0];
  } else if (postNumber !== undefined) {
    phase = [5, Number.parseInt(postNumber, 10), 0, 0];
  } else {
    phase = [4, 0, 0, 0];
  }
  return { release, phase };
}

function padRelease(release: number[], length: number): number[] {
  return [...release, ...Array.from({ length: length - release.length }, () => 0)];
}

export function compareVersions(left: string, right: string): -1 | 0 | 1 {
  const parsedLeft = parseVersion(left);
  const parsedRight = parseVersion(right);
  const length = Math.max(parsedLeft.release.length, parsedRight.release.length);
  const leftRelease = padRelease(parsedLeft.release, length);
  const rightRelease = padRelease(parsedRight.release, length);
  for (let i = 0; i < length; i += 1) {
    if (leftRelease[i] !== rightRelease[i]) {
      return leftRelease[i] < rightRelease[i] ? -1 : 1;
    }
  }
  for (let i = 0; i < 4; i += 1) {
    if (parsedLeft.phase[i] !== parsedRight.phase[i]) {
      return parsedLeft.phase[i] < parsedRight.phase[i] ? -1 : 1;
    }
  }
  return 0;
}

function hasSameRelease(left: string, right: string): boolean {
  const parsedLeft = parseVersion(left);
  const parsedRight = parseVersion(right);
  const length = Math.max(parsedLeft.release.length, parsedRight.release.length);
  const key = (release: number[]) => padRelease(release, length).join(".");
  return key(parsedLeft.release) === key(parsedRight.release);
}

export interface CompatibilityResult {
  status: CompatibilityStatus;
  mainProgramVersion: string;
  webuiVersion: string;
  requiredWebuiVersion: string;
}

export function getWebuiVersionCompatibility(
  webuiVersion: string,
  projectVersion: string,
  requiredWebuiVersion: string,
): CompatibilityResult {
  const comparison = compareVersions(webuiVersion, requiredWebuiVersion);
  let status: CompatibilityStatus;
  if (comparison < 0) {
    status = "webui_outdated";
  } else if (!hasSameRelease(webuiVersion, requiredWebuiVersion)) {
    status = "main_program_outdated";
  } else {
    status = "compatible";
  }
  return {
    status,
    mainProgramVersion: projectVersion,
    webuiVersion,
    requiredWebuiVersion,
  };
}
