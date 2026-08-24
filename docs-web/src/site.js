import { routes } from "vue-router/auto-routes";

/**
 * The nav is DERIVED from the file routes, never hand-listed. A page file is
 * the single source of truth: add one and it appears, delete one and it goes.
 * Hand-maintaining this list is how a nav entry once pointed at a page that did
 * not exist, which renders as a blank pane and reports nothing anywhere.
 *
 * META only decorates a path that already exists. An unlisted route still
 * appears, titled from its filename.
 */
const META = {
  "/framework": { title: "What is on the die", short: "Overview", order: 0 },
  "/framework/noc": { title: "Mesh and routers", short: "NoC", order: 1 },
  "/framework/sysnode": {
    title: "The system node",
    short: "Sysnode",
    order: 2,
  },
  "/framework/cpu": {
    title: "The RV32 core, and what the framework gives it",
    short: "CPU",
    order: 3,
  },
  "/framework/ship": { title: "Ship assembly", short: "Ship", order: 4 },
  "/framework/physical": {
    title: "Floorplan and clocks",
    short: "Physical",
    order: 5,
  },
  "/framework/axi": {
    title: "AXI and the station bus",
    short: "AXI",
    order: 6,
  },
  "/framework/measurements": {
    title: "Out-of-context measurements",
    short: "Measurements",
    order: 7,
  },

  "/tpu": { title: "The accelerator", short: "Overview", order: 0 },
  "/tpu/matmul": { title: "Matmul cluster", short: "Matmul", order: 1 },
  "/tpu/matmul/microarchitecture": {
    title: "Matmul cluster — microarchitecture",
    short: "Matmul micro",
    order: 2,
  },
  "/tpu/vector": { title: "Vector core", short: "Vector", order: 3 },
  "/tpu/vector/microarchitecture": {
    title: "Vector core — microarchitecture",
    short: "Vector micro",
    order: 4,
  },
  "/tpu/memory": {
    title: "Residency and accumulators",
    short: "Memory",
    order: 5,
  },
  "/tpu/numbers": {
    title: "MXFP7 and the dtype ladder",
    short: "Numbers",
    order: 6,
  },
  "/tpu/results": { title: "What was measured", short: "Results", order: 7 },

  "/component": {
    title: "The parts the framework ships",
    short: "Overview",
    order: 0,
    domain: "cpu",
  },
  "/component/cpu/microarchitecture": {
    title: "CPU PE — microarchitecture",
    short: "CPU PE micro",
    order: 1,
    domain: "cpu",
  },
  "/component/sysnode/microarchitecture": {
    title: "System node — microarchitecture",
    short: "Sysnode micro",
    order: 2,
    domain: "cpu",
  },
  "/component/caching": {
    title: "Staging, the transform slot and the tagged L2",
    short: "Caching",
    order: 3,
    domain: "cpu",
  },

  "/mpe": {
    title: "A mesh of processors",
    short: "Overview",
    order: 0,
    domain: "simt",
  },
  "/mpe/hetero": {
    title: "SIMD, SIMT and the two KohakuTPU units",
    short: "Heterogeneity",
    order: 1,
    domain: "simt",
  },
  "/mpe/simd": { title: "SIMD PE", short: "SIMD PE", order: 2, domain: "simd" },
  "/mpe/simd/microarchitecture": {
    title: "SIMD PE — microarchitecture",
    short: "SIMD PE micro",
    order: 3,
    domain: "simd",
  },
  "/mpe/simt": { title: "SIMT PE", short: "SIMT PE", order: 4, domain: "simt" },
  "/mpe/simt/microarchitecture": {
    title: "SIMT PE — microarchitecture",
    short: "SIMT micro",
    order: 5,
    domain: "simt",
  },
  "/mpe/simt/comparison": {
    title: "Where this lands against shipped GPUs",
    short: "SIMT vs industry",
    order: 6,
    domain: "simt",
  },
  "/mpe/measurements": {
    title: "SIMT PE measurements",
    short: "Measurements",
    order: 7,
    domain: "simt",
  },
};

const SECTION_DEF = {
  framework: {
    title: "Framework",
    domain: "framework",
    icon: "i-carbon-chip",
    blurb: "Everything around the compute unit you design.",
  },
  component: {
    title: "Framework component",
    domain: "cpu",
    icon: "i-carbon-cube",
    blurb: "Parts the framework ships working, and expects you to replace.",
  },
  tpu: {
    title: "KohakuTPU",
    domain: "tpu",
    icon: "i-carbon-matrix",
    blurb: "The reference accelerator: an MXFP7 tensor engine.",
  },
  mpe: {
    title: "KohakuMPE",
    domain: "simt",
    icon: "i-carbon-cpu",
    blurb: "A mesh whose compute units are processors.",
  },
};
const SECTION_ORDER = ["framework", "component", "tpu", "mpe"];

/** Every routable path with a component, parent paths joined. */
function walk(list, base = "") {
  const out = [];
  for (const r of list) {
    const p = r.path.startsWith("/")
      ? r.path
      : `${base}/${r.path}`.replace(/\/+/g, "/");
    const clean = p.length > 1 ? p.replace(/\/$/, "") : p;
    if (r.components || r.component) out.push(clean);
    if (r.children?.length) out.push(...walk(r.children, clean));
  }
  return [...new Set(out)];
}

const titleFrom = (path) => {
  const leaf = path.split("/").filter(Boolean).pop() ?? "Home";
  return leaf.replace(/-/g, " ").replace(/^\w/, (c) => c.toUpperCase());
};

const ALL = walk(routes).filter((p) => p !== "/" && !p.includes(":"));

export const SECTIONS = SECTION_ORDER.map((key) => {
  const def = SECTION_DEF[key];
  const pages = ALL.filter((p) => p === `/${key}` || p.startsWith(`/${key}/`))
    .map((path) => {
      const m = META[path] ?? {};
      return {
        path,
        title: m.title ?? titleFrom(path),
        short: m.short ?? titleFrom(path),
        domain: m.domain ?? def.domain,
        order: m.order ?? 99,
      };
    })
    .sort((a, b) => a.order - b.order || a.path.localeCompare(b.path));
  return { key, ...def, pages };
}).filter((s) => s.pages.length);

export const ALL_PAGES = SECTIONS.flatMap((s) =>
  s.pages.map((p) => ({ ...p, section: s.key })),
);

export function pageFor(path) {
  return ALL_PAGES.find((p) => p.path === path);
}
