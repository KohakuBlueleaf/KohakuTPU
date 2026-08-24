/**
 * Gemstone color constants for programmatic use.
 * UnoCSS theme handles CSS classes; this is for JS-driven coloring (SVG diagrams).
 */

export const GEM = {
  sapphire: { light: "#D6E3F8", main: "#0F52BA", shadow: "#082567" },
  aquamarine: { light: "#D4EDE8", main: "#4C9989", shadow: "#1B6B5A" },
  taaffeite: { light: "#E8D5ED", main: "#A57EAE", shadow: "#6B4670" },
  iolite: { light: "#DDD0F0", main: "#5A4FCF", shadow: "#312A7A" },
  amber: { light: "#F5E6C8", main: "#D4920A", shadow: "#8B5E00" },
  coral: { light: "#F5D5D5", main: "#D46B6B", shadow: "#8B3A3A" },
  sage: { light: "#D5E8DA", main: "#5A9E6F", shadow: "#3A6B48" },
};

/** One gem per documentation domain. A page declares its domain once. */
export const DOMAIN_GEM = {
  framework: "sapphire",
  tpu: "amber",
  cpu: "aquamarine",
  dsp: "taaffeite",
  gpu: "iolite",
};

export function gemFor(domain) {
  return GEM[DOMAIN_GEM[domain] ?? domain] ?? GEM.iolite;
}

/** Inline style that rebinds --gem-* for a subtree. */
export function gemVars(domain) {
  const g = gemFor(domain);
  return {
    "--gem-light": g.light,
    "--gem-main": g.main,
    "--gem-shadow": g.shadow,
  };
}

/** Maturity of a documented thing, mapped to the functional gems. */
export function statusColor(status) {
  switch (status) {
    case "shipped":
    case "measured":
      return GEM.sage;
    case "building":
    case "planned":
      return GEM.amber;
    case "broken":
    case "retired":
      return GEM.coral;
    default:
      return GEM.amber;
  }
}
