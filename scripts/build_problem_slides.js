const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const PptxGenJS = require('pptxgenjs');

const ROOT = path.join(__dirname, '..');
const OUT_DIR = path.join(ROOT, 'deliverables', 'problem_graphs');
const DECK_PATH = path.join(ROOT, 'deliverables', 'Farms_for_Food_problem_slides.pptx');
const SVG_W = 2400;
const SVG_H = 1350;

const C = {
  ink: '#132524',
  pine: '#143B37',
  deep: '#0E302D',
  teal: '#1D6B62',
  aqua: '#78B7A9',
  coral: '#B94735',
  blush: '#F1D5CC',
  cream: '#F4F0E6',
  paper: '#FFFDF8',
  line: '#D9D2C2',
  muted: '#61706D',
  mist: '#E8EEE9',
  amber: '#C88C2C',
  amberBg: '#F7E9CB',
  white: '#FFFFFF',
};

const sources = {
  fdaCounts: 'https://api.fda.gov/food/enforcement.json?search=recall_initiation_date:%5B20240101%20TO%2020241231%5D&count=event_id&limit=1000',
  fdaRecords: 'https://api.fda.gov/food/enforcement.json?search=recall_initiation_date:%5B20240101%20TO%2020241231%5D&limit=1',
  fsis: 'https://www.fsis.usda.gov/fsis/api/recall/v/1',
  onions: 'https://www.fda.gov/food/outbreaks-foodborne-illness/outbreak-investigation-e-coli-o157h7-onions-october-2024',
  foodBank: 'https://www.sfmfoodbank.org/annual-report-2023-2024/',
  foodSecurity: 'https://www.ers.usda.gov/topics/food-nutrition-assistance/food-security-in-the-us/key-statistics-graphics',
};

function esc(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function text(x, y, value, size, options = {}) {
  const {
    color = C.ink,
    weight = 400,
    anchor = 'start',
    family = 'Aptos, Carlito, Arial, sans-serif',
    spacing = 0,
    opacity = 1,
    style = '',
  } = options;
  return `<text x="${x}" y="${y}" fill="${color}" font-family="${family}" font-size="${size}" font-weight="${weight}" text-anchor="${anchor}" letter-spacing="${spacing}" opacity="${opacity}" style="${style}">${esc(value)}</text>`;
}

function lines(x, y, values, size, lineHeight, options = {}) {
  const {
    color = C.ink,
    weight = 400,
    anchor = 'start',
    family = 'Aptos, Carlito, Arial, sans-serif',
    spacing = 0,
  } = options;
  const tspans = values.map((value, index) => `<tspan x="${x}" dy="${index === 0 ? 0 : lineHeight}">${esc(value)}</tspan>`).join('');
  return `<text x="${x}" y="${y}" fill="${color}" font-family="${family}" font-size="${size}" font-weight="${weight}" text-anchor="${anchor}" letter-spacing="${spacing}">${tspans}</text>`;
}

function rect(x, y, w, h, fill, radius = 0, stroke = 'none', strokeWidth = 0) {
  return `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${radius}" fill="${fill}" stroke="${stroke}" stroke-width="${strokeWidth}"/>`;
}

function circle(cx, cy, r, fill, stroke = 'none', strokeWidth = 0) {
  return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${fill}" stroke="${stroke}" stroke-width="${strokeWidth}"/>`;
}

function line(x1, y1, x2, y2, color, width = 4, dash = '') {
  return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${color}" stroke-width="${width}" stroke-linecap="round"${dash ? ` stroke-dasharray="${dash}"` : ''}/>`;
}

function header(kicker, dark = false) {
  const fg = dark ? C.paper : C.pine;
  const logoFill = dark ? 'none' : C.pine;
  const logoStroke = dark ? C.paper : C.pine;
  const logoText = C.paper;
  return [
    rect(96, 56, 68, 68, logoFill, 0, logoStroke, 3),
    text(130, 101, 'FFF', 25, { color: logoText, weight: 700, anchor: 'middle' }),
    text(188, 101, 'Farms for Food', 30, { color: fg, weight: 700 }),
    text(2304, 101, kicker.toUpperCase(), 20, { color: dark ? '#D7E7E1' : C.coral, weight: 700, anchor: 'end', spacing: 3 }),
  ].join('');
}

function footer(sourceLabel, dark = false) {
  return [
    line(96, 1262, 2304, 1262, dark ? '#48635E' : C.line, 2),
    text(96, 1305, sourceLabel, 21, { color: dark ? '#D7E7E1' : C.muted }),
    text(2304, 1305, 'PROBLEM EVIDENCE · NO PRODUCT OR DEMO DATA', 19, { color: dark ? C.aqua : C.teal, weight: 700, anchor: 'end', spacing: 1.3 }),
  ].join('');
}

function svgFrame(content, background) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${SVG_W}" height="${SVG_H}" viewBox="0 0 ${SVG_W} ${SVG_H}">${rect(0, 0, SVG_W, SVG_H, background)}${content}</svg>`;
}

function polar(cx, cy, radius, angleDegrees) {
  const angle = (angleDegrees - 90) * Math.PI / 180;
  return { x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) };
}

function donutArc(cx, cy, radius, startAngle, endAngle, color, width) {
  const start = polar(cx, cy, radius, endAngle);
  const end = polar(cx, cy, radius, startAngle);
  const largeArc = endAngle - startAngle <= 180 ? 0 : 1;
  return `<path d="M ${start.x} ${start.y} A ${radius} ${radius} 0 ${largeArc} 0 ${end.x} ${end.y}" fill="none" stroke="${color}" stroke-width="${width}" stroke-linecap="round"/>`;
}

function recallActivitySvg() {
  const fdaMax = 1387;
  const fsisMax = 34;
  const panelY = 370;
  const panelH = 760;
  const fdaX = 96;
  const fsisX = 1238;
  const panelW = 1066;
  const barW = 830;
  const fdaEventW = barW * 482 / fdaMax;
  const fsisPhaW = barW * 19 / fsisMax;

  const content = [
    header('Problem 01 · Recall load'),
    text(96, 212, 'Food-safety incidents created a sustained operating load in 2024.', 69, { weight: 700 }),
    text(96, 286, 'Public federal data captured 1,387 FDA product records plus dozens of FSIS actions during the year.', 31, { color: C.muted }),

    rect(fdaX, panelY, panelW, panelH, C.pine, 28),
    text(fdaX + 56, panelY + 72, 'FDA FOOD ENFORCEMENT · 2024', 23, { color: C.aqua, weight: 700, spacing: 2.2 }),
    text(fdaX + 56, panelY + 160, '1,387', 78, { color: C.paper, weight: 700 }),
    text(fdaX + 340, panelY + 154, 'product records', 31, { color: C.paper, weight: 700 }),
    text(fdaX + 340, panelY + 194, 'initiated during the year', 23, { color: '#D7E7E1' }),
    text(fdaX + 56, panelY + 288, 'Product records', 22, { color: C.paper, weight: 700 }),
    rect(fdaX + 56, panelY + 315, barW, 54, '#48635E', 27),
    rect(fdaX + 56, panelY + 315, barW, 54, C.coral, 27),
    text(fdaX + 56 + barW + 24, panelY + 354, '1,387', 26, { color: C.paper, weight: 700 }),
    text(fdaX + 56, panelY + 438, 'Distinct event IDs', 22, { color: C.paper, weight: 700 }),
    rect(fdaX + 56, panelY + 465, barW, 54, '#48635E', 27),
    rect(fdaX + 56, panelY + 465, fdaEventW, 54, C.aqua, 27),
    text(fdaX + 56 + fdaEventW + 24, panelY + 504, '482', 26, { color: C.paper, weight: 700 }),
    rect(fdaX + 56, panelY + 592, panelW - 112, 140, '#0E302D', 18, '#48635E', 2),
    text(fdaX + 84, panelY + 636, 'Why the units matter', 21, { color: C.aqua, weight: 700 }),
    lines(fdaX + 84, panelY + 674, ['One FDA enforcement event can contain multiple', 'product records. These figures are related—not additive.'], 21, 29, { color: C.paper }),

    rect(fsisX, panelY, panelW, panelH, C.paper, 28, C.line, 3),
    text(fsisX + 56, panelY + 72, 'USDA FSIS · 2024', 23, { color: C.coral, weight: 700, spacing: 2.2 }),
    text(fsisX + 56, panelY + 160, '34', 72, { color: C.ink, weight: 700 }),
    lines(fsisX + 190, panelY + 146, ['numbered', 'recall cases'], 25, 32, { color: C.ink, weight: 700 }),
    line(fsisX + 506, panelY + 112, fsisX + 506, panelY + 210, C.line, 3),
    text(fsisX + 560, panelY + 160, '19', 72, { color: C.ink, weight: 700 }),
    lines(fsisX + 696, panelY + 146, ['Public Health', 'Alerts'], 25, 32, { color: C.ink, weight: 700 }),
    text(fsisX + 56, panelY + 288, 'Numbered recall cases', 22, { color: C.ink, weight: 700 }),
    rect(fsisX + 56, panelY + 315, barW, 54, C.mist, 27),
    rect(fsisX + 56, panelY + 315, barW, 54, C.teal, 27),
    text(fsisX + 56 + barW + 24, panelY + 354, '34', 26, { color: C.ink, weight: 700 }),
    text(fsisX + 56, panelY + 438, 'Public Health Alerts', 22, { color: C.ink, weight: 700 }),
    rect(fsisX + 56, panelY + 465, barW, 54, C.mist, 27),
    rect(fsisX + 56, panelY + 465, fsisPhaW, 54, C.amber, 27),
    text(fsisX + 56 + fsisPhaW + 24, panelY + 504, '19', 26, { color: C.ink, weight: 700 }),
    rect(fsisX + 56, panelY + 592, panelW - 112, 140, C.mist, 18),
    text(fsisX + 84, panelY + 636, 'Do not collapse the categories', 21, { color: C.teal, weight: 700 }),
    lines(fsisX + 84, panelY + 674, ['A Public Health Alert is not a numbered recall.', 'FSIS and FDA also use different reporting units.'], 21, 29, { color: C.ink }),

    footer('Sources: openFDA Food Enforcement API; USDA FSIS Recall API · Calendar year 2024 · Retrieved 17 Jul 2026'),
  ].join('');
  return svgFrame(content, C.cream);
}

function outbreakSvg() {
  const cx = 1810;
  const cy = 710;
  const content = [
    header('Problem 02 · Blast radius', true),
    text(96, 212, 'One ingredient reached at least 12 states—and cases appeared in 14.', 62, { color: C.paper, weight: 700 }),
    text(96, 286, 'FDA: recalled yellow onions were the likely source based on epidemiologic and traceback evidence.', 29, { color: '#D7E7E1' }),

    text(112, 388, 'HOW THE INCIDENT EXPANDED', 21, { color: C.aqua, weight: 700, spacing: 2.4 }),
    line(206, 458, 206, 1070, '#48635E', 8),
    circle(206, 500, 27, C.aqua),
    text(270, 488, 'SEP 27', 21, { color: C.aqua, weight: 700 }),
    text(270, 532, 'First reported illness onset', 31, { color: C.paper, weight: 700 }),
    text(270, 569, 'Illnesses were later reported across 14 states.', 22, { color: '#D7E7E1' }),

    circle(206, 710, 27, C.coral),
    text(270, 698, 'OCT 22', 21, { color: '#E8A497', weight: 700 }),
    text(270, 742, 'Taylor Farms initiates a voluntary recall', 31, { color: C.paper, weight: 700 }),
    lines(270, 780, ['Yellow onions had reached food-service customers in 12 confirmed states;', 'McDonald’s stopped using recalled onions in affected locations.'], 22, 32, { color: '#D7E7E1' }),

    circle(206, 962, 27, C.teal, C.paper, 3),
    text(270, 950, 'DEC 3', 21, { color: C.aqua, weight: 700 }),
    text(270, 994, 'Outbreak declared over', 31, { color: C.paper, weight: 700 }),
    text(270, 1031, 'FDA investigation closed after traceback, sampling, and customer action.', 22, { color: '#D7E7E1' }),

    rect(1370, 388, 934, 742, C.deep, 30, '#48635E', 3),
    circle(cx, cy, 255, C.teal),
    text(cx, cy - 20, '104', 112, { color: C.paper, weight: 700, anchor: 'middle' }),
    text(cx, cy + 36, 'reported cases', 29, { color: C.paper, weight: 700, anchor: 'middle' }),
    text(cx, cy + 92, '14 states', 25, { color: '#D7E7E1', weight: 700, anchor: 'middle' }),
    rect(1430, 990, 610, 88, C.coral, 44),
    text(1735, 1047, '34 hospitalized among 98 with known status', 22, { color: C.white, weight: 700, anchor: 'middle' }),
    rect(2070, 990, 180, 88, '#7F2D23', 44),
    text(2160, 1047, '1 death', 24, { color: C.white, weight: 700, anchor: 'middle' }),

    footer('Source: FDA, Outbreak Investigation of E. coli O157:H7: Onions · Data current 3 Dec 2024', true),
  ].join('');
  return svgFrame(content, C.pine);
}

function networkSvg() {
  const cx = 645;
  const cy = 735;
  const freshRate = 0.70;
  const content = [
    header('Problem 03 · Network fragility'),
    text(96, 212, 'One regional food bank moved 67M pounds through a wide service network.', 62, { weight: 700 }),
    text(96, 286, 'SF–Marin Food Bank, FY2024: nearly 70% fresh produce, 215 pantries, and 53K households served weekly.', 30, { color: C.muted }),

    rect(96, 368, 1098, 770, C.pine, 30),
    text(152, 432, 'PRODUCT MIX', 21, { color: C.aqua, weight: 700, spacing: 2.2 }),
    circle(cx, cy, 265, 'none', '#48635E', 92),
    donutArc(cx, cy, 265, 0, 360 * freshRate, C.coral, 92),
    text(cx, cy - 26, '67M', 111, { color: C.paper, weight: 700, anchor: 'middle' }),
    text(cx, cy + 31, 'lb distributed', 29, { color: C.paper, weight: 700, anchor: 'middle' }),
    text(cx, cy + 85, 'FY2024', 24, { color: C.aqua, weight: 700, anchor: 'middle' }),
    rect(202, 1044, 886, 66, '#0E302D', 33),
    circle(250, 1077, 12, C.coral),
    text(280, 1086, 'Nearly 70% fresh produce', 24, { color: C.paper, weight: 700 }),
    circle(705, 1077, 12, '#48635E'),
    text(735, 1086, '≈30% other food', 24, { color: '#D7E7E1' }),

    text(1292, 432, 'SERVICE NETWORK', 21, { color: C.coral, weight: 700, spacing: 2.2 }),
    text(1292, 504, 'ILLUSTRATIVE FLOW · NOT TO SCALE', 20, { color: C.muted, weight: 700, spacing: 1.8 }),
    rect(1292, 620, 280, 220, C.teal, 28),
    text(1432, 716, 'FOOD BANK', 31, { color: C.white, weight: 700, anchor: 'middle' }),
    text(1432, 762, 'SOURCE + SORT', 20, { color: '#D7E7E1', weight: 700, anchor: 'middle', spacing: 1.2 }),
    text(1607, 757, '→', 58, { color: C.coral, weight: 700, anchor: 'middle' }),
    rect(1642, 620, 330, 220, C.mist, 28, C.teal, 3),
    text(1807, 710, '215', 72, { color: C.ink, weight: 700, anchor: 'middle' }),
    text(1807, 764, 'NEIGHBORHOOD PANTRIES', 21, { color: C.teal, weight: 700, anchor: 'middle' }),
    text(2007, 757, '→', 58, { color: C.coral, weight: 700, anchor: 'middle' }),
    rect(2042, 620, 262, 220, C.blush, 28, C.coral, 3),
    text(2173, 700, '53K', 67, { color: C.red || '#8F3327', weight: 700, anchor: 'middle' }),
    text(2173, 754, 'HOUSEHOLDS', 22, { color: C.ink, weight: 700, anchor: 'middle' }),
    text(2173, 786, 'SERVED WEEKLY', 18, { color: C.muted, weight: 700, anchor: 'middle' }),
    text(1292, 902, 'Reported network totals; the flow is illustrative, not a count of individual routes.', 20, { color: C.muted }),
    rect(1292, 968, 1012, 142, C.mist, 22),
    text(1334, 1016, 'Why recalls become operational disruptions', 23, { color: C.teal, weight: 700 }),
    lines(1334, 1056, ['Perishable inventory is already moving through many handoffs.', 'Removing one item can affect downstream commitments immediately.'], 22, 31, { color: C.ink }),

    footer('Source: San Francisco–Marin Food Bank, Annual Report 2023–2024 · FY2024 figures'),
  ].join('');
  return svgFrame(content, C.cream);
}

function pressureSvg() {
  const maxPct = 20;
  const chartX = 910;
  const chartW = 1330;
  const rows = [
    { y: 526, label: 'All U.S. households · food insecure', value: 13.7, count: '18.3M households', color: C.teal },
    { y: 748, label: 'Households with children · food insecure', value: 18.4, count: '6.7M households', color: C.coral },
    { y: 970, label: 'All U.S. households · very low food security', value: 5.4, count: '7.2M households', color: C.amber, labelColor: C.ink },
  ];
  const content = [
    header('Problem 04 · Demand pressure'),
    text(96, 212, 'Food insecurity remained elevated in 2024.', 68, { weight: 700 }),
    text(96, 286, 'USDA household food-security estimates for calendar year 2024.', 31, { color: C.muted }),

    rect(96, 376, 690, 748, C.pine, 30),
    text(150, 454, 'PEOPLE LIVING IN', 21, { color: C.aqua, weight: 700, spacing: 2.0 }),
    text(150, 492, 'FOOD-INSECURE HOUSEHOLDS', 21, { color: C.aqua, weight: 700, spacing: 2.0 }),
    text(150, 660, '47.9M', 124, { color: C.paper, weight: 700 }),
    lines(150, 735, ['people in 2024', '—about one in seven people'], 31, 43, { color: C.paper, weight: 700 }),
    line(150, 862, 690, 862, '#48635E', 3),
    text(150, 924, 'The 2024 household rate was', 23, { color: '#D7E7E1' }),
    text(150, 965, 'significantly higher', 31, { color: C.amberBg, weight: 700 }),
    lines(150, 1007, ['than every annual prevalence', 'reported from 2016 through 2021.'], 23, 33, { color: '#D7E7E1' }),

    text(chartX, 420, 'SHARE OF HOUSEHOLDS · 0–20% SCALE', 21, { color: C.coral, weight: 700, spacing: 2.2 }),
    ...[0, 5, 10, 15, 20].map((tick) => {
      const x = chartX + chartW * tick / maxPct;
      return `${line(x, 472, x, 1060, C.line, 2, '7 10')}${text(x, 1094, `${tick}%`, 19, { color: C.muted, anchor: 'middle' })}`;
    }),
    ...rows.map((row) => {
      const w = chartW * row.value / maxPct;
      return [
        text(chartX, row.y - 42, row.label, 28, { color: C.ink, weight: 700 }),
        text(chartX + chartW, row.y - 42, row.count, 22, { color: C.muted, weight: 700, anchor: 'end' }),
        rect(chartX, row.y, chartW, 64, C.mist, 32),
        rect(chartX, row.y, w, 64, row.color, 32),
        text(chartX + w - 22, row.y + 44, `${row.value}%`, 28, { color: row.labelColor || C.white, weight: 700, anchor: 'end' }),
      ].join('');
    }),
    rect(910, 1142, 1330, 64, C.amberBg, 18),
    text(946, 1183, '“Very low” means disrupted eating patterns and reduced food intake because resources ran short.', 21, { color: C.ink, weight: 700 }),

    footer('Source: USDA Economic Research Service, Household Food Security in the United States in 2024'),
  ].join('');
  return svgFrame(content, C.paper);
}

const slides = [
  {
    file: '01_recall_activity',
    title: 'Food-safety incidents created a sustained operating load in 2024',
    svg: recallActivitySvg(),
    notes: `Sources and definitions:\n- FDA product records: ${sources.fdaRecords}\n- FDA distinct event IDs: ${sources.fdaCounts}\n- USDA FSIS recalls and Public Health Alerts: ${sources.fsis}\nCalendar-year 2024. FDA product records and distinct event IDs are related, not additive. FSIS numbered recalls and Public Health Alerts are different action types. For the FSIS totals, translated duplicate rows and -EXP expansion records were normalized to the base recall number before counting 34 distinct numbered recall cases and 19 distinct Public Health Alerts. Counts retrieved July 17, 2026.`,
  },
  {
    file: '02_outbreak_blast_radius',
    title: 'One ingredient reached at least 12 states and cases appeared in 14',
    svg: outbreakSvg(),
    notes: `Primary source: ${sources.onions}\nFDA reported 104 cases, 34 known hospitalizations, one death, and cases in 14 states; hospitalization status was available for 98 cases. Recalled onions had confirmed distribution in 12 states. Taylor Farms initiated the voluntary recall on October 22, 2024; FDA closed the investigation and CDC declared the outbreak over on December 3, 2024. FDA described recalled yellow onions as the likely source based on epidemiologic and traceback evidence.`,
  },
  {
    file: '03_food_bank_network_fragility',
    title: 'One regional food bank moved 67M pounds through a wide service network',
    svg: networkSvg(),
    notes: `Primary source: ${sources.foodBank}\nSan Francisco–Marin Food Bank FY2024 figures: 67 million pounds distributed; nearly 70 percent fresh produce; 53,000 households served weekly; 215 weekly neighborhood food pantries. The approximately 30 percent “other food” segment is the visual complement of the reported “nearly 70 percent” fresh-produce share, not a separately reported figure. This slide uses one regional food bank as a scale and perishability example, not as a national estimate.`,
  },
  {
    file: '04_food_insecurity_pressure',
    title: 'Food insecurity remained elevated in 2024',
    svg: pressureSvg(),
    notes: `Primary source: ${sources.foodSecurity}\nUSDA ERS 2024 estimates: 47.9 million people lived in food-insecure households; 13.7 percent (18.3 million) of all U.S. households were food insecure; 18.4 percent (6.7 million) of households with children were food insecure; 5.4 percent (7.2 million) of all households had very low food security. USDA reports the 2024 prevalence was significantly higher than annual prevalence from 2016 through 2021.`,
  },
];

function writeAssets() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  for (const slide of slides) {
    const svgPath = path.join(OUT_DIR, `${slide.file}.svg`);
    const pngPath = path.join(OUT_DIR, `${slide.file}.png`);
    fs.writeFileSync(svgPath, slide.svg);
    execFileSync('magick', [svgPath, pngPath], { stdio: 'inherit' });
    slide.pngPath = pngPath;
  }
}

function validateBounds(pptx) {
  const issues = [];
  for (const [slideIndex, slide] of pptx._slides.entries()) {
    for (const [objectIndex, obj] of (slide._slideObjects || []).entries()) {
      const o = obj.options || obj._options || {};
      if ([o.x, o.y, o.w, o.h].every((value) => typeof value === 'number')) {
        if (o.x < -0.01 || o.y < -0.01 || o.x + o.w > 13.343 || o.y + o.h > 7.51) {
          issues.push(`slide ${slideIndex + 1}, object ${objectIndex + 1}: ${o.x},${o.y},${o.w},${o.h}`);
        }
      }
    }
  }
  if (issues.length) throw new Error(`Objects outside slide bounds:\n${issues.join('\n')}`);
}

async function writeDeck() {
  const pptx = new PptxGenJS();
  pptx.layout = 'LAYOUT_WIDE';
  pptx.author = 'Farms for Food';
  pptx.company = 'Farms for Food';
  pptx.subject = 'Problem evidence: recall load, outbreak blast radius, food-bank fragility, and food insecurity';
  pptx.title = 'Farms for Food — problem evidence graphs';
  pptx.lang = 'en-US';
  pptx.theme = { headFontFace: 'Aptos Display', bodyFontFace: 'Aptos', lang: 'en-US' };

  for (const item of slides) {
    const slide = pptx.addSlide();
    slide.background = { color: 'F4F0E6' };
    slide.addImage({
      path: item.pngPath,
      x: 0,
      y: 0,
      w: 13.333,
      h: 7.5,
      altText: item.title,
    });
    slide.addNotes(item.notes);
  }

  validateBounds(pptx);
  await pptx.writeFile({ fileName: DECK_PATH, compression: true });
}

async function build() {
  writeAssets();
  await writeDeck();
  console.log(`Wrote ${slides.length} graph pairs to ${OUT_DIR}`);
  console.log(`Wrote ${DECK_PATH}`);
}

build().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
