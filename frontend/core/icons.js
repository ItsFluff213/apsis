// Vessel category vocabulary: the type list, display labels, colours, and
// the inline SVG icon for each.
//
// The icons are hand-drawn to actually read as the thing they represent (a
// rocket, a dish-and-panels satellite, a capsule) rather than being
// arbitrary polygons you have to learn to associate with a category.
// `{c}` is substituted with the category colour at render time.

export const VESSEL_TYPES = [
  "unknown", "booster", "satellite", "docking", "station", "capsule", "lander", "probe",
];

export const CATEGORY_ORDER = [
  "booster", "satellite", "docking", "station", "capsule", "lander", "probe", "unknown",
];

export const CATEGORY_LABELS = {
  booster: "Boosters", satellite: "Satellites", docking: "Docking / Cargo", station: "Stations",
  capsule: "Capsules", lander: "Landers", probe: "Probes", unknown: "Unsorted",
};

export const CATEGORY_COLORS = {
  booster: "#ff8a4d", satellite: "#4da3ff", docking: "#f472b6", station: "#c084fc",
  capsule: "#4dd28c", lander: "#ffd24d", probe: "#7d8aa8", unknown: "#dbe4f5",
};

const CATEGORY_ICONS = {
  booster: `
    <path d="M12 2c2.4 2 3.4 5.4 3.4 9.2 0 2.6-.5 4.9-1.3 6.8h-4.2c-.8-1.9-1.3-4.2-1.3-6.8C8.6 7.4 9.6 4 12 2z" fill="{c}"/>
    <path d="M8.6 13.5 5 17.5l2.4-.6 1.6-2.3z" fill="{c}"/>
    <path d="M15.4 13.5 19 17.5l-2.4-.6-1.6-2.3z" fill="{c}"/>
    <circle cx="12" cy="9" r="1.3" fill="#0e1420"/>
    <path d="M10.4 18h3.2l-.5 3.2a1.1 1.1 0 0 1-2.2 0z" fill="{c}"/>`,
  lander: `
    <rect x="8.5" y="6" width="7" height="7" rx="1.3" fill="{c}"/>
    <circle cx="12" cy="9.5" r="1.6" fill="#0e1420"/>
    <path d="M9 12.5 5.5 20M15 12.5 18.5 20M7.7 12.5 6.2 20M16.3 12.5 17.8 20" stroke="{c}" stroke-width="1.4" fill="none" stroke-linecap="round"/>
    <path d="M4.8 20h2.2M17 20h2.2" stroke="{c}" stroke-width="1.4" stroke-linecap="round"/>`,
  satellite: `
    <rect x="9.3" y="9.3" width="5.4" height="5.4" rx="1" fill="{c}"/>
    <path d="M2.5 6.5 8 9v6l-5.5 2.5z" fill="{c}"/>
    <path d="M21.5 6.5 16 9v6l5.5 2.5z" fill="{c}"/>
    <path d="M15 9 20 4M20 4h-2.6M20 4v2.6" stroke="{c}" stroke-width="1.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`,
  station: `
    <rect x="4" y="10.5" width="16" height="3" rx="1" fill="{c}"/>
    <circle cx="12" cy="12" r="4.6" fill="none" stroke="{c}" stroke-width="1.6"/>
    <rect x="10.5" y="2.5" width="3" height="4" rx="1" fill="{c}"/>
    <rect x="10.5" y="17.5" width="3" height="4" rx="1" fill="{c}"/>`,
  capsule: `
    <path d="M8 21 6.5 12A5.5 6 0 0 1 12 3a5.5 6 0 0 1 5.5 9L16 21z" fill="{c}"/>
    <circle cx="12" cy="10.5" r="1.8" fill="#0e1420"/>`,
  probe: `
    <rect x="8.5" y="9" width="7" height="7" rx="1.5" fill="{c}"/>
    <path d="M12 9V4M12 4 9.5 2M12 4l2.5-2" stroke="{c}" stroke-width="1.3" fill="none" stroke-linecap="round"/>
    <path d="M5.5 12h3M15.5 12h3" stroke="{c}" stroke-width="1.4" stroke-linecap="round"/>`,
  docking: `
    <circle cx="12" cy="12" r="9" fill="none" stroke="{c}" stroke-width="2.2"/>
    <circle cx="12" cy="12" r="4" fill="{c}"/>`,
  unknown: `<circle cx="12" cy="12" r="8" fill="{c}"/>`,
};

export function svgIconFor(category, color) {
  const body = (CATEGORY_ICONS[category] || CATEGORY_ICONS.unknown).replace(/\{c\}/g, color);
  return `<svg width="16" height="16" viewBox="0 0 24 24">${body}</svg>`;
}

export function colorFor(category) {
  return CATEGORY_COLORS[category] || CATEGORY_COLORS.unknown;
}

export function categoryOf(vessel) {
  return VESSEL_TYPES.includes(vessel.type) ? vessel.type : "unknown";
}
