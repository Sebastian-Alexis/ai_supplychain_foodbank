const path = require('node:path');
const PptxGenJS = require('pptxgenjs');
const QRCode = require('qrcode');

const pptx = new PptxGenJS();
pptx.layout = 'LAYOUT_WIDE';
pptx.author = 'FoodShock';
pptx.company = 'FoodShock';
pptx.subject = 'Human-approved food-bank recall response and recovery planning';
pptx.title = 'FoodShock — recall response command center';
pptx.lang = 'en-US';
pptx.theme = {
  headFontFace: 'Aptos Display',
  bodyFontFace: 'Aptos',
  lang: 'en-US',
};
pptx.defineSlideMaster({
  title: 'FOODSHOCK',
  background: { color: 'F4F0E6' },
  objects: [
    { rect: { x: 0, y: 0, w: 0.18, h: 7.5, fill: { color: 'B94735' }, line: { color: 'B94735' } } },
    { text: { text: 'FS', options: { x: 0.68, y: 0.28, w: 0.38, h: 0.3, fontFace: 'Aptos', fontSize: 10, bold: true, align: 'center', valign: 'mid', color: 'FFFDF8', fill: { color: '143B37' }, margin: 0 } } },
    { text: { text: 'FoodShock', options: { x: 1.13, y: 0.26, w: 1.25, h: 0.32, fontFace: 'Aptos', fontSize: 11, bold: true, color: '143B37', margin: 0 } } },
  ],
  slideNumber: { x: 12.52, y: 7.04, w: 0.24, h: 0.2, color: '61706D', fontFace: 'Aptos', fontSize: 8, align: 'right', margin: 0 },
});

const C = {
  ink: '132524',
  pine: '143B37',
  deep: '0E302D',
  teal: '1D6B62',
  aqua: '78B7A9',
  coral: 'B94735',
  blush: 'F1D5CC',
  cream: 'F4F0E6',
  paper: 'FFFDF8',
  line: 'D9D2C2',
  muted: '61706D',
  mist: 'E8EEE9',
  amber: 'C88C2C',
  amberBg: 'F7E9CB',
  slate: '64757A',
  white: 'FFFFFF',
  red: '8F3327',
};

const SH = pptx.ShapeType;
const appUrl = 'https://foodshock.sebastianalexis.com';
const repoUrl = 'https://github.com/Sebastian-Alexis/ai_supplychain_foodbank';
const asset = (name) => path.join(__dirname, '..', 'deliverables', 'assets', name);
const output = path.join(__dirname, '..', 'deliverables', 'FoodShock_pitch_deck.pptx');

function addText(slide, text, x, y, w, h, opts = {}) {
  slide.addText(text, {
    x, y, w, h,
    fontFace: 'Aptos',
    fontSize: 15,
    color: C.ink,
    margin: 0,
    breakLine: false,
    valign: 'mid',
    fit: 'shrink',
    ...opts,
  });
}

function addTitle(slide, title, kicker) {
  if (kicker) {
    addText(slide, kicker.toUpperCase(), 0.72, 0.72, 5.4, 0.25, {
      fontSize: 10.5,
      bold: true,
      charSpacing: 1.2,
      color: C.coral,
    });
  }
  addText(slide, title, 0.72, kicker ? 1.02 : 0.78, 11.85, 0.65, {
    fontFace: 'Aptos Display',
    fontSize: 36,
    bold: true,
    color: C.ink,
  });
}

function addSourceFooter(slide, sources, note = '') {
  const runs = [];
  if (note) runs.push({ text: `${note}  `, options: { color: C.muted } });
  sources.forEach((source, index) => {
    if (index > 0) runs.push({ text: '  |  ', options: { color: C.line } });
    runs.push({
      text: source.label,
      options: {
        color: C.teal,
        underline: { color: C.teal },
        hyperlink: { url: source.url, tooltip: source.label },
      },
    });
  });
  slide.addText(runs, {
    x: 0.72, y: 7.02, w: 11.45, h: 0.23,
    fontFace: 'Aptos', fontSize: 8.5, margin: 0,
    valign: 'mid', fit: 'shrink',
  });
}

function addCard(slide, x, y, w, h, options = {}) {
  slide.addShape(SH.roundRect, {
    x, y, w, h,
    rectRadius: 0.08,
    fill: { color: options.fill || C.paper, transparency: options.transparency || 0 },
    line: { color: options.line || C.line, width: options.lineWidth || 1 },
    shadow: options.shadow === false ? undefined : { type: 'outer', color: '596864', opacity: 0.12, blur: 1, angle: 45, distance: 1 },
  });
}

function addMetricCard(slide, x, y, w, h, value, label, accent = C.teal, sub = '') {
  addCard(slide, x, y, w, h, { fill: C.paper, line: C.line, shadow: false });
  slide.addShape(SH.rect, { x, y, w: 0.08, h, fill: { color: accent }, line: { color: accent } });
  addText(slide, value, x + 0.2, y + 0.16, w - 0.35, 0.45, {
    fontFace: 'Aptos Display', fontSize: 25, bold: true, color: C.ink,
  });
  addText(slide, label, x + 0.2, y + 0.63, w - 0.35, 0.34, {
    fontSize: 10.5, bold: true, color: C.muted,
  });
  if (sub) {
    addText(slide, sub, x + 0.2, y + h - 0.33, w - 0.35, 0.22, {
      fontSize: 8.5, color: C.muted,
    });
  }
}

function addPill(slide, text, x, y, w, fill, color = C.white) {
  slide.addShape(SH.roundRect, { x, y, w, h: 0.3, fill: { color: fill }, line: { color: fill } });
  addText(slide, text, x + 0.08, y + 0.03, w - 0.16, 0.23, {
    fontSize: 9, bold: true, align: 'center', color,
  });
}

function addStep(slide, x, y, number, title, body, color = C.teal) {
  slide.addShape(SH.ellipse, { x, y, w: 0.48, h: 0.48, fill: { color }, line: { color } });
  addText(slide, String(number), x, y + 0.03, 0.48, 0.38, { fontSize: 13, bold: true, align: 'center', color: C.white });
  addText(slide, title, x + 0.66, y - 0.02, 1.55, 0.28, { fontSize: 14, bold: true, color: C.ink });
  addText(slide, body, x + 0.66, y + 0.27, 1.62, 0.55, { fontSize: 10.5, color: C.muted, valign: 'top' });
}

function addCheckRow(slide, x, y, w, label, detail, color = C.teal) {
  slide.addShape(SH.ellipse, { x, y: y + 0.02, w: 0.26, h: 0.26, fill: { color }, line: { color } });
  addText(slide, '✓', x, y + 0.01, 0.26, 0.24, { fontSize: 10, bold: true, align: 'center', color: C.white });
  addText(slide, label, x + 0.38, y, w - 0.38, 0.26, { fontSize: 11.5, bold: true });
  addText(slide, detail, x + 0.38, y + 0.27, w - 0.38, 0.3, { fontSize: 9.5, color: C.muted, valign: 'top' });
}

function addArrow(slide, x1, y1, x2, y2, color = C.line, width = 1.5) {
  slide.addShape(SH.line, {
    x: x1, y: y1, w: x2 - x1, h: y2 - y1,
    line: { color, width, beginArrowType: 'none', endArrowType: 'triangle' },
  });
}

function addNotes(slide, text) {
  slide.addNotes(text);
}

function validateBounds() {
  const W = 13.333;
  const H = 7.5;
  const issues = [];
  pptx._slides.forEach((slide, slideIndex) => {
    (slide._slideObjects || []).forEach((obj, objectIndex) => {
      const o = obj.options || obj._options || {};
      if ([o.x, o.y, o.w, o.h].every((v) => typeof v === 'number')) {
        if (o.x < -0.01 || o.y < -0.01 || o.x + o.w > W + 0.01 || o.y + o.h > H + 0.01) {
          issues.push(`slide ${slideIndex + 1}, object ${objectIndex + 1}: out of bounds (${o.x},${o.y},${o.w},${o.h})`);
        }
      }
    });
  });
  if (issues.length) throw new Error(`Deck geometry validation failed:\n${issues.join('\n')}`);
}

async function build() {
  const qrData = await QRCode.toDataURL(appUrl, {
    margin: 1,
    width: 520,
    color: { dark: '#143B37', light: '#FFFDF8' },
  });

  // 1 — Title
  {
    const slide = pptx.addSlide();
    slide.background = { color: C.pine };
    slide.addShape(SH.rect, { x: 0, y: 0, w: 0.18, h: 7.5, fill: { color: C.coral }, line: { color: C.coral } });
    slide.addShape(SH.rect, { x: 7.95, y: 0, w: 5.38, h: 7.5, fill: { color: C.deep }, line: { color: C.deep } });
    slide.addShape(SH.rect, { x: 0.72, y: 0.6, w: 0.48, h: 0.48, fill: { color: C.paper, transparency: 100 }, line: { color: C.paper, width: 1 } });
    addText(slide, 'FS', 0.72, 0.65, 0.48, 0.32, { fontSize: 11, bold: true, align: 'center', color: C.paper });
    addText(slide, 'FoodShock', 1.38, 0.63, 1.8, 0.36, { fontSize: 15, bold: true, color: C.paper });
    addText(slide, 'RECALL RESPONSE COMMAND CENTER', 0.72, 1.55, 5.8, 0.3, { fontSize: 10.5, bold: true, charSpacing: 1.5, color: C.aqua });
    addText(slide, 'From recall notice to\nhuman-approved recovery plan.', 0.72, 1.95, 7.0, 1.42, {
      fontFace: 'Aptos Display', fontSize: 37, bold: true, color: C.paper, valign: 'top', breakLine: true,
    });
    addText(slide, 'Trace implicated lots and inbound orders, quantify seven-day supply risk, then optimize a feasible response before anything executes.', 0.72, 3.78, 6.55, 1.0, {
      fontSize: 18, color: 'D7E7E1', breakLine: true, valign: 'top', paraSpaceAfterPt: 6,
    });

    const flowX = [8.45, 9.55, 10.65, 11.75];
    const flow = [
      ['NOTICE', 'extract'],
      ['TRACE', 'lots + POs'],
      ['REPLAN', 'LP + FEFO'],
      ['APPROVE', 'human gate'],
    ];
    flow.forEach(([label, sub], i) => {
      slide.addShape(SH.ellipse, { x: flowX[i], y: 1.47, w: 0.68, h: 0.68, fill: { color: i === 3 ? C.coral : C.teal }, line: { color: C.paper, transparency: 45, width: 1 } });
      addText(slide, String(i + 1), flowX[i], 1.62, 0.68, 0.27, { fontSize: 14, bold: true, align: 'center', color: C.white });
      addText(slide, label, flowX[i] - 0.19, 2.28, 1.06, 0.26, { fontSize: 10, bold: true, align: 'center', color: C.paper });
      addText(slide, sub, flowX[i] - 0.25, 2.56, 1.18, 0.24, { fontSize: 8.5, align: 'center', color: C.aqua });
      if (i < flow.length - 1) addArrow(slide, flowX[i] + 0.72, 1.81, flowX[i + 1] - 0.04, 1.81, C.aqua, 1.3);
    });

    addMetricCard(slide, 8.45, 3.55, 1.3, 1.45, '0.81s', 'planning time', C.aqua, 'latest seeded run');
    addMetricCard(slide, 9.95, 3.55, 1.3, 1.45, '+1,800', 'lb served', C.coral, 'vs baseline');
    addMetricCard(slide, 11.45, 3.55, 1.3, 1.45, '0', 'violations', C.aqua, 'hard constraints');
    addPill(slide, 'LIVE DEMO', 8.45, 5.55, 1.1, C.coral);
    addText(slide, 'foodshock.sebastianalexis.com', 9.72, 5.53, 3.02, 0.35, { fontSize: 10.5, bold: true, color: C.paper, hyperlink: { url: appUrl } });
    addText(slide, 'Synthetic operations data. No client PII. No operator validation claimed.', 0.72, 6.92, 6.9, 0.25, { fontSize: 9, color: C.aqua });
    addText(slide, '1', 12.52, 7.03, 0.24, 0.2, { fontSize: 8, color: C.aqua, align: 'right' });
    addNotes(slide, 'FoodShock is a decision-support command center for food-bank recall response. The core promise is not autonomous execution: it is faster, traceable analysis ending at a human approval gate. All operational data in this demonstration is synthetic.');
  }

  // 2 — Why now
  {
    const slide = pptx.addSlide('FOODSHOCK');
    addTitle(slide, 'Recall volume is high; consequences are operational.', 'Why now');
    addText(slide, 'Public 2024 records show the scale. A single contaminated ingredient can intersect a perishable, high-throughput food-bank network.', 0.72, 1.72, 11.8, 0.58, { fontSize: 16, color: C.muted, valign: 'top' });

    addMetricCard(slide, 0.72, 2.55, 2.25, 1.58, '482', 'distinct FDA event IDs', C.coral, '1,387 product records initiated in 2024');
    addMetricCard(slide, 3.2, 2.55, 2.25, 1.58, '34 + 19', 'FSIS recalls + PHAs', C.teal, 'numbered recalls and public-health alerts');
    addMetricCard(slide, 5.68, 2.55, 2.25, 1.58, '104', 'onion-outbreak cases', C.coral, '34 hospitalized, 1 death, 14 states');

    addCard(slide, 8.35, 2.55, 4.28, 3.75, { fill: C.pine, line: C.pine, shadow: false });
    addText(slide, 'A reference food-bank network', 8.78, 2.9, 3.4, 0.34, { fontSize: 14, bold: true, color: C.paper });
    addText(slide, '67M lb', 8.78, 3.45, 2.0, 0.52, { fontFace: 'Aptos Display', fontSize: 31, bold: true, color: C.paper });
    addText(slide, 'distributed in FY2024', 10.63, 3.56, 1.42, 0.3, { fontSize: 10.5, color: C.aqua });
    addText(slide, 'Fresh product share', 8.78, 4.26, 2.4, 0.25, { fontSize: 10.5, bold: true, color: C.paper });
    slide.addShape(SH.roundRect, { x: 8.78, y: 4.62, w: 3.35, h: 0.35, fill: { color: '48635E' }, line: { color: '48635E' } });
    slide.addShape(SH.roundRect, { x: 8.78, y: 4.62, w: 2.35, h: 0.35, fill: { color: C.coral }, line: { color: C.coral } });
    addText(slide, '~70%', 10.28, 4.61, 0.72, 0.29, { fontSize: 12, bold: true, align: 'right', color: C.paper });
    addText(slide, '53k', 8.78, 5.28, 1.1, 0.39, { fontSize: 23, bold: true, color: C.paper });
    addText(slide, 'households / week', 8.78, 5.72, 1.55, 0.26, { fontSize: 9.3, color: C.aqua });
    addText(slide, '215', 10.95, 5.28, 0.85, 0.39, { fontSize: 23, bold: true, color: C.paper });
    addText(slide, 'pantries', 10.95, 5.72, 0.9, 0.26, { fontSize: 9.3, color: C.aqua });

    addText(slide, 'The constraint is not only finding recalled product. It is restoring service without reintroducing risk.', 0.72, 5.0, 7.2, 0.85, { fontFace: 'Aptos Display', fontSize: 22, bold: true, color: C.ink, valign: 'top' });
    addPill(slide, 'SAFETY', 0.72, 6.15, 0.92, C.coral);
    addText(slide, 'Quarantine, cancellation, and lot-state exclusions must survive every scenario and plan.', 1.82, 6.11, 6.1, 0.42, { fontSize: 13.5, color: C.muted });
    addSourceFooter(slide, [
      { label: 'openFDA enforcement API', url: 'https://api.fda.gov/food/enforcement.json?search=recall_initiation_date:%5B20240101%2BTO%2B20241231%5D&count=event_id&limit=1000' },
      { label: 'FSIS recalls API', url: 'https://www.fsis.usda.gov/fsis/api/recall/v/1' },
      { label: 'CDC/FDA onion outbreak', url: 'https://www.cdc.gov/ecoli/outbreaks/e-coli-O157.html' },
      { label: 'SF-Marin Food Bank FY2024', url: 'https://www.sfmfoodbank.org/annual-report-2023-2024/' },
    ], 'Counts retrieved for calendar year 2024; event/product distinction preserved.');
    addNotes(slide, 'The public-record counts are evidence of volume, not a claim about food-bank-specific incident frequency. The network scale comes from the SF-Marin Food Bank annual report and illustrates why perishable inventory and distributed pantry demand complicate recovery.');
  }

  // 3 — Operational blind spot
  {
    const slide = pptx.addSlide('FOODSHOCK');
    addTitle(slide, 'The operational blind spot is the join.', 'Problem');
    addText(slide, 'Recall notices describe risk in prose. Response teams must connect that prose to exact inventory, inbound purchase orders, pantry commitments, and feasible replacements.', 0.72, 1.72, 11.7, 0.62, { fontSize: 16, color: C.muted, valign: 'top' });

    addCard(slide, 0.72, 2.55, 3.0, 3.62, { fill: C.paper, line: C.line, shadow: false });
    addPill(slide, 'NARRATIVE NOTICE', 1.02, 2.85, 1.42, C.coral);
    addText(slide, '“Fresh yellow onions … lot codes GVP-8842 and GVP-8843 … packed between June 20 and July 2 … distributed in California and Nevada.”', 1.02, 3.42, 2.38, 1.65, { fontSize: 16, italic: true, color: C.ink, valign: 'top' });
    addText(slide, 'Supplier, lot, date, region, and action are embedded—not normalized.', 1.02, 5.35, 2.35, 0.48, { fontSize: 10.5, color: C.muted, valign: 'top' });

    addArrow(slide, 3.92, 4.25, 4.65, 4.25, C.coral, 2.2);
    addText(slide, 'JOIN', 4.0, 3.83, 0.58, 0.24, { fontSize: 9.5, bold: true, align: 'center', color: C.coral });

    addCard(slide, 4.82, 2.55, 7.8, 3.62, { fill: C.pine, line: C.pine, shadow: false });
    const stepXs = [5.18, 6.62, 8.06, 9.5, 10.94];
    const steps = [
      ['01', 'Product', 'fresh yellow onions'],
      ['02', 'Lot / PO', 'GVP-8842, PO-1002'],
      ['03', 'Warehouse', 'Oakland DC'],
      ['04', 'Pantry', '28 lines infeasible'],
      ['05', 'Recovery', 'substitute + buy'],
    ];
    steps.forEach(([n, title, body], i) => {
      if (i < steps.length - 1) addArrow(slide, stepXs[i] + 1.05, 3.58, stepXs[i + 1] - 0.08, 3.58, C.aqua, 1.4);
      slide.addShape(SH.ellipse, { x: stepXs[i], y: 3.15, w: 0.84, h: 0.84, fill: { color: i === 4 ? C.coral : C.teal }, line: { color: C.paper, transparency: 55 } });
      addText(slide, n, stepXs[i], 3.37, 0.84, 0.28, { fontSize: 12, bold: true, align: 'center', color: C.white });
      addText(slide, title, stepXs[i] - 0.16, 4.24, 1.17, 0.28, { fontSize: 11, bold: true, align: 'center', color: C.paper });
      addText(slide, body, stepXs[i] - 0.28, 4.58, 1.43, 0.57, { fontSize: 9.3, align: 'center', color: C.aqua, valign: 'top' });
    });
    addText(slide, 'Missing joins create three failure modes', 5.18, 5.38, 3.4, 0.3, { fontSize: 12, bold: true, color: C.paper });
    addPill(slide, 'DELAYED QUARANTINE', 5.18, 5.73, 1.55, C.teal);
    addPill(slide, 'INFEASIBLE ALLOCATIONS', 6.93, 5.73, 1.78, C.teal);
    addPill(slide, 'SILENT SHORTAGES', 8.91, 5.73, 1.46, C.coral);

    addSourceFooter(slide, [
      { label: 'Rhode Island Community Food Bank recall process', url: 'https://rifoodbank.org/wp-content/uploads/2023/06/Food-Safety-Recall-Process-doc.pdf' },
      { label: 'MANNA FoodBank recall workflow', url: 'https://mannafoodbank.org/agency-access-and-information/product-recalls/' },
    ], 'Workflow sources show notice intake, inventory isolation, agency notification, and disposition tasks.');
    addNotes(slide, 'The job is not summarization. It is a relational join across notice fields and operational records, followed by constrained recovery planning. FoodShock makes each join inspectable.');
  }

  // 4 — Architecture and guardrails
  {
    const slide = pptx.addSlide('FOODSHOCK');
    addTitle(slide, 'Agentic analysis. Deterministic execution boundary.', 'System');
    addText(slide, 'The agent gathers and explains evidence. Typed tools, state projection, optimization, and approval checks decide what is feasible and what may execute.', 0.72, 1.72, 11.7, 0.58, { fontSize: 16, color: C.muted, valign: 'top' });

    addText(slide, 'AGENT LOOP', 0.72, 2.55, 1.28, 0.25, { fontSize: 10, bold: true, charSpacing: 1.1, color: C.coral });
    const loop = [
      ['Observe', 'notice + state'],
      ['Extract', 'typed fields'],
      ['Trace', 'graph joins'],
      ['Project', '7-day service'],
      ['Optimize', 'LP recovery'],
      ['Explain', 'draft + comms'],
    ];
    loop.forEach(([title, body], i) => {
      const x = 0.72 + i * 1.48;
      addCard(slide, x, 2.95, 1.26, 1.0, { fill: i === 5 ? C.blush : C.paper, line: i === 5 ? C.coral : C.line, shadow: false });
      addText(slide, title, x + 0.08, 3.13, 1.1, 0.25, { fontSize: 11.2, bold: true, color: i === 5 ? C.red : C.ink, align: 'center' });
      addText(slide, body, x + 0.07, 3.48, 1.12, 0.3, { fontSize: 8.8, color: C.muted, align: 'center' });
      if (i < loop.length - 1) addArrow(slide, x + 1.28, 3.45, x + 1.45, 3.45, C.teal, 1.3);
    });

    addCard(slide, 0.72, 4.46, 8.92, 1.7, { fill: C.pine, line: C.pine, shadow: false });
    addText(slide, 'DETERMINISTIC GUARDRAIL LANE', 1.03, 4.75, 3.15, 0.25, { fontSize: 10, bold: true, charSpacing: 1.1, color: C.aqua });
    const guards = [
      ['Provenance', 'unsupported fields dropped'],
      ['Recall state', 'confirmed inventory never returns'],
      ['Projection', 'warehouse FEFO + PO ETA'],
      ['Optimization', 'capacity, demand, expiry, cost'],
      ['Approval', 'latest, intact, feasible, idempotent'],
    ];
    guards.forEach(([title, body], i) => {
      const x = 1.03 + i * 1.64;
      slide.addShape(SH.ellipse, { x, y: 5.2, w: 0.26, h: 0.26, fill: { color: i === 4 ? C.coral : C.aqua }, line: { color: i === 4 ? C.coral : C.aqua } });
      addText(slide, title, x + 0.38, 5.15, 1.13, 0.24, { fontSize: 10, bold: true, color: C.paper });
      addText(slide, body, x + 0.38, 5.43, 1.16, 0.5, { fontSize: 8.3, color: C.aqua, valign: 'top' });
    });

    addCard(slide, 9.98, 2.55, 2.65, 3.61, { fill: C.mist, line: C.line, shadow: false });
    addText(slide, 'Boring stack, on purpose', 10.28, 2.87, 2.05, 0.32, { fontSize: 13.5, bold: true });
    addCheckRow(slide, 10.28, 3.44, 2.05, 'SQLite', 'auditable incident state');
    addCheckRow(slide, 10.28, 4.13, 2.05, 'NetworkX', 'recall-to-pantry lineage');
    addCheckRow(slide, 10.28, 4.82, 2.05, 'Linear program', 'feasible recovery plan');
    addCheckRow(slide, 10.28, 5.51, 2.05, 'Streamlit', 'review and approval UI', C.coral);
    addSourceFooter(slide, [
      { label: 'Architecture and source', url: `${repoUrl}#readme` },
      { label: 'Approval implementation', url: `${repoUrl}/blob/main/foodshock/engine.py` },
    ], 'LLM narration can be cached/template-only; execution safety does not depend on free-form text.');
    addNotes(slide, 'The architectural boundary is deliberate: language-model output cannot directly mutate inventory or approve a plan. Approval revalidates plan identity, payload integrity, and hard constraints inside the state layer.');
  }

  // 5 — Exposure proof
  {
    const slide = pptx.addSlide('FOODSHOCK');
    addTitle(slide, 'Every claim can be traced back to evidence.', 'Product proof');
    addText(slide, 'The exposure view pairs typed incident facts with verbatim source excerpts, then follows exact lot and purchase-order lineage into the operating network.', 0.72, 1.72, 11.7, 0.55, { fontSize: 15.5, color: C.muted, valign: 'top' });

    addCard(slide, 0.72, 2.48, 8.55, 4.15, { fill: C.paper, line: C.line, shadow: false });
    slide.addImage({
      path: asset('foodshock-public-exposure-crop.png'),
      x: 0.86, y: 2.62, w: 8.27, h: 3.87,
      altText: 'FoodShock exposure queue showing incident facts and supporting source excerpts',
    });

    addCard(slide, 9.62, 2.48, 3.0, 4.15, { fill: C.mist, line: C.teal, shadow: false });
    addText(slide, 'Evidence contract', 9.96, 2.84, 2.25, 0.32, { fontSize: 15, bold: true, color: C.ink });
    addCheckRow(slide, 9.96, 3.4, 2.25, 'Original notice retained', 'raw text remains inspectable', C.teal);
    addCheckRow(slide, 9.96, 4.14, 2.25, 'Field-level quote', 'each value has supporting text', C.teal);
    addCheckRow(slide, 9.96, 4.88, 2.25, 'Unsupported value dropped', 'verifier removes weak provenance', C.coral);
    addCheckRow(slide, 9.96, 5.62, 2.25, 'Exact operational join', 'lot or inbound PO drives impact', C.teal);
    addSourceFooter(slide, [
      { label: 'Live exposure queue', url: appUrl },
      { label: 'Extraction verifier', url: `${repoUrl}/blob/main/foodshock/extraction.py` },
    ], 'Screenshot captured from the public synthetic-data demo.');
    addNotes(slide, 'The screenshot is not a mockup. It is the public Streamlit deployment. Verbatim excerpts remain alongside structured fields so a reviewer can challenge extraction before acting.');
  }

  // 6 — Results
  {
    const slide = pptx.addSlide('FOODSHOCK');
    addTitle(slide, 'The recovery plan restores service without breaking safety.', 'Measured demo run');
    addText(slide, 'Baseline and recommended scenarios use the same seven-day synthetic demand. Confirmed recall states are excluded from both.', 0.72, 1.72, 11.6, 0.48, { fontSize: 15.5, color: C.muted });

    addCard(slide, 0.72, 2.42, 7.25, 3.95, { fill: C.paper, line: C.line, shadow: false });
    addText(slide, 'Seven-day service outcome (lb)', 1.05, 2.72, 4.2, 0.32, { fontSize: 13, bold: true });
    slide.addChart(pptx.ChartType.bar, [
      { name: 'Do nothing', labels: ['Served', 'Unmet'], values: [10122.5, 2477.5] },
      { name: 'Recommended', labels: ['Served', 'Unmet'], values: [11922.5, 677.5] },
    ], {
      x: 1.0, y: 3.13, w: 6.62, h: 2.8,
      catAxisLabelFontFace: 'Aptos', catAxisLabelFontSize: 11,
      valAxisLabelFontFace: 'Aptos', valAxisLabelFontSize: 9,
      showTitle: false, showLegend: true, legendPos: 'b', legendFontFace: 'Aptos', legendFontSize: 10,
      showValue: true, dataLabelPosition: 'outEnd', dataLabelFontFace: 'Aptos', dataLabelFontSize: 9,
      valAxisLabelFormatCode: '#,##0', dataLabelFormatCode: '#,##0',
      chartColors: [C.slate, C.teal],
      showCatName: false, showSerName: false,
      showValue: true,
      showValAxisTitle: false, showCatAxisTitle: false,
      showValAxis: true, showCatAxis: true,
      showGridLines: true, gridLine: { color: C.line, width: 1 },
      valAxisMinVal: 0, valAxisMaxVal: 13000, valAxisMajorUnit: 2500,
      showBorder: false,
    });

    addMetricCard(slide, 8.32, 2.42, 2.02, 1.15, '86.1%', 'worst pantry fill', C.teal, 'from 64.3%');
    addMetricCard(slide, 10.58, 2.42, 2.02, 1.15, '0', 'boxes disrupted', C.teal, 'from 180');
    addMetricCard(slide, 8.32, 3.83, 2.02, 1.15, '$990', 'procurement cost', C.amber, 'recommended plan');
    addMetricCard(slide, 10.58, 3.83, 2.02, 1.15, '7+', 'produce days', C.teal, 'from 4.8');
    addMetricCard(slide, 8.32, 5.24, 2.02, 1.15, '0', 'hard violations', C.teal, 'approval gate');
    addMetricCard(slide, 10.58, 5.24, 2.02, 1.15, '0.81s', 'planning time', C.teal, 'latest seeded run');

    addSourceFooter(slide, [
      { label: 'Deterministic demo runner', url: `${repoUrl}/blob/main/foodshock/demo.py` },
      { label: 'Scenario database', url: `${repoUrl}/tree/main/data` },
    ], 'Measured locally on the synthetic scenario. No comparison to the hypothetical 2.0 staff-hour task model is claimed.');
    addNotes(slide, 'The recommended plan adds 1,800 pounds served, cuts unmet demand by the same amount, raises the worst pantry fill rate by 21.8 percentage points, and eliminates 180 disrupted boxes for 990 dollars. The runtime is a measured software run; the manual-time comparator remains hypothetical and is intentionally excluded from the chart.');
  }

  // 7 — Human approval
  {
    const slide = pptx.addSlide('FOODSHOCK');
    addTitle(slide, 'Human approval is a transaction, not a button.', 'Control');
    addText(slide, 'The UI presents a draft plan and its tradeoffs. Approval revalidates plan currency and feasibility, then writes one auditable state transition.', 0.72, 1.72, 11.65, 0.55, { fontSize: 15.5, color: C.muted, valign: 'top' });

    addCard(slide, 0.72, 2.48, 8.55, 4.15, { fill: C.paper, line: C.line, shadow: false });
    slide.addImage({
      path: asset('foodshock-public-plan-crop.png'),
      x: 0.86, y: 2.62, w: 8.27, h: 3.87,
      altText: 'FoodShock recovery plan showing service, unmet demand, cost, disruptions, and hard-constraint violations',
    });

    addCard(slide, 9.62, 2.48, 3.0, 2.48, { fill: C.mist, line: C.line, shadow: false });
    addText(slide, 'Approval gates', 9.96, 2.76, 2.22, 0.3, { fontSize: 14, bold: true });
    addCheckRow(slide, 9.96, 3.22, 2.25, 'Latest recommendation', 'stale drafts rejected');
    addCheckRow(slide, 9.96, 3.78, 2.25, 'Plan lines re-evaluated', 'offers, timing, constraints checked');
    addCheckRow(slide, 9.96, 4.34, 2.25, 'Zero hard violations', 'state checked again', C.coral);

    addCard(slide, 9.62, 5.22, 3.0, 1.41, { fill: C.pine, line: C.pine, shadow: false });
    addText(slide, 'After approval', 9.96, 5.48, 2.22, 0.27, { fontSize: 13, bold: true, color: C.paper });
    addText(slide, 'Lot-specific pantry notices\nPurchase instructions\nAuditable approval event', 9.96, 5.84, 2.2, 0.62, { fontSize: 10.3, color: C.aqua, valign: 'top', breakLine: true, paraSpaceAfterPt: 4 });

    addSourceFooter(slide, [
      { label: 'Live recovery plan', url: appUrl },
      { label: 'Approval regression tests', url: `${repoUrl}/blob/main/tests/test_agent.py` },
    ], 'Screenshot captured from the public synthetic-data demo.');
    addNotes(slide, 'Approval is idempotent, rejects unknown or stale plans, revalidates plan lines, and re-checks hard constraints. Communications are scoped to implicated lot IDs instead of broadcasting generic recall language to unaffected recipients.');
  }

  // 8 — Evidence status
  {
    const slide = pptx.addSlide('FOODSHOCK');
    addTitle(slide, 'Evidence is separated from ambition.', 'Validation status');
    addText(slide, 'Measured behavior, evaluation readiness, and future validation are reported as different categories. No unavailable result is converted into a claim.', 0.72, 1.72, 11.7, 0.52, { fontSize: 15.5, color: C.muted, valign: 'top' });

    const cols = [
      {
        x: 0.72, fill: C.mist, line: C.teal, tag: 'MEASURED', tagColor: C.teal,
        title: 'In the synthetic demo',
        items: [
          ['0.81s', 'latest seeded run'],
          ['0', 'hard-constraint violations'],
          ['+1,800 lb', 'served vs baseline'],
          ['8 tests', 'new safety regressions'],
        ],
      },
      {
        x: 4.89, fill: C.amberBg, line: C.amber, tag: 'READY / NOT RUN', tagColor: C.amber,
        title: 'Extraction evaluation',
        items: [
          ['5 notices', 'frozen official gold set'],
          ['Exact match', 'field-level scorer'],
          ['Provenance', 'raw + verified outputs'],
          ['No rate', 'Anthropic credential unavailable'],
        ],
      },
      {
        x: 9.06, fill: 'E9ECEC', line: C.slate, tag: 'NOT YET VALIDATED', tagColor: C.slate,
        title: 'In live operations',
        items: [
          ['0 interviews', 'food-bank operator discovery'],
          ['No timing study', 'manual response comparator'],
          ['No client data', 'production performance'],
          ['Pilot needed', 'workflow and impact validation'],
        ],
      },
    ];
    cols.forEach((col) => {
      addCard(slide, col.x, 2.58, 3.55, 3.95, { fill: col.fill, line: col.line, lineWidth: 1.4, shadow: false });
      addPill(slide, col.tag, col.x + 0.32, 2.9, 1.55, col.tagColor);
      addText(slide, col.title, col.x + 0.32, 3.38, 2.85, 0.33, { fontSize: 14.5, bold: true });
      col.items.forEach(([value, label], i) => {
        const y = 3.98 + i * 0.59;
        addText(slide, value, col.x + 0.32, y, 1.22, 0.29, { fontSize: 12.5, bold: true, color: col.tagColor });
        addText(slide, label, col.x + 1.72, y + 0.01, 1.47, 0.3, { fontSize: 9.6, color: C.ink, valign: 'top' });
      });
    });
    slide.addShape(SH.roundRect, { x: 5.21, y: 6.13, w: 3.05, h: 0.28, fill: { color: C.amber }, line: { color: C.amber } });
    addText(slide, 'MODEL ACCURACY / NOT CLAIMED', 5.34, 6.16, 2.79, 0.2, { fontSize: 9.1, bold: true, align: 'center', color: C.white });
    addSourceFooter(slide, [
      { label: 'Frozen gold set', url: `${repoUrl}/blob/main/data/eval/gold.json` },
      { label: 'Evaluation runner', url: `${repoUrl}/blob/main/foodshock/eval_extraction.py` },
      { label: 'Safety tests', url: `${repoUrl}/tree/main/tests` },
    ], 'Published evidence is a fallback for discovery, not a substitute for operator interviews.');
    addNotes(slide, 'The deck does not report extraction accuracy because a valid Anthropic credential was unavailable. The gold set and scorer are committed so the run can be reproduced once access exists. The operational results are also explicitly synthetic and not operator-validated.');
  }

  // 9 — Close
  {
    const slide = pptx.addSlide();
    slide.background = { color: C.pine };
    slide.addShape(SH.rect, { x: 0, y: 0, w: 0.18, h: 7.5, fill: { color: C.coral }, line: { color: C.coral } });
    slide.addShape(SH.rect, { x: 0.72, y: 0.58, w: 0.42, h: 0.42, fill: { color: C.paper, transparency: 100 }, line: { color: C.paper, width: 1 } });
    addText(slide, 'FS', 0.72, 0.64, 0.42, 0.25, { fontSize: 9.5, bold: true, align: 'center', color: C.paper });
    addText(slide, 'FoodShock', 1.3, 0.61, 1.5, 0.3, { fontSize: 13, bold: true, color: C.paper });
    addText(slide, 'Pilot one recall.\nProve the decision loop.', 0.72, 1.2, 6.55, 1.35, { fontFace: 'Aptos Display', fontSize: 39, bold: true, color: C.paper, breakLine: true, valign: 'top' });
    addText(slide, 'Start with one notice type, one warehouse, and one pantry network. Measure trace completeness, planning time, reviewer corrections, and service recovery.', 0.72, 2.84, 6.2, 0.88, { fontSize: 17, color: 'D7E7E1', valign: 'top' });

    const pilot = [
      ['01', 'Connect', 'inventory, purchase orders, allocations'],
      ['02', 'Shadow', 'run beside the current recall process'],
      ['03', 'Measure', 'safety, speed, reviewer confidence'],
    ];
    pilot.forEach(([n, title, body], i) => {
      const y = 4.2 + i * 0.75;
      slide.addShape(SH.ellipse, { x: 0.72, y, w: 0.42, h: 0.42, fill: { color: C.teal }, line: { color: C.teal } });
      addText(slide, n, 0.72, y + 0.08, 0.42, 0.22, { fontSize: 9.5, bold: true, align: 'center', color: C.white });
      addText(slide, title, 1.34, y - 0.01, 1.0, 0.25, { fontSize: 13, bold: true, color: C.paper });
      addText(slide, body, 2.35, y, 3.8, 0.27, { fontSize: 11, color: C.aqua });
    });

    addCard(slide, 8.0, 0.92, 4.62, 5.86, { fill: C.paper, line: C.paper, shadow: false });
    addText(slide, 'Open the live command center', 8.45, 1.35, 3.72, 0.37, { fontSize: 16, bold: true, align: 'center' });
    slide.addImage({ data: qrData, x: 9.22, y: 1.98, w: 2.18, h: 2.18, hyperlink: { url: appUrl }, altText: 'QR code opening the FoodShock live demo' });
    addText(slide, 'foodshock.sebastianalexis.com', 8.45, 4.42, 3.72, 0.34, { fontSize: 12.5, bold: true, align: 'center', color: C.teal, hyperlink: { url: appUrl } });
    addText(slide, 'Source code', 8.45, 5.12, 1.2, 0.26, { fontSize: 10, bold: true, color: C.muted });
    addText(slide, 'github.com/Sebastian-Alexis/\nai_supplychain_foodbank', 9.58, 5.09, 2.55, 0.52, { fontSize: 10.2, color: C.teal, hyperlink: { url: repoUrl }, breakLine: true });
    addPill(slide, 'SYNTHETIC DATA', 8.45, 6.05, 1.45, C.coral);
    addText(slide, 'No client PII. Human approval required.', 10.08, 6.04, 2.05, 0.31, { fontSize: 9.5, color: C.muted });

    addText(slide, [
      { text: 'Sources: ', options: { bold: true, color: C.aqua } },
      { text: 'openFDA', options: { color: C.paper, hyperlink: { url: 'https://api.fda.gov/food/enforcement.json' } } },
      { text: '  |  ', options: { color: '48635E' } },
      { text: 'FSIS', options: { color: C.paper, hyperlink: { url: 'https://www.fsis.usda.gov/recalls' } } },
      { text: '  |  ', options: { color: '48635E' } },
      { text: 'CDC/FDA', options: { color: C.paper, hyperlink: { url: 'https://www.cdc.gov/ecoli/outbreaks/e-coli-O157.html' } } },
      { text: '  |  ', options: { color: '48635E' } },
      { text: 'SF-Marin', options: { color: C.paper, hyperlink: { url: 'https://www.sfmfoodbank.org/annual-report-2023-2024/' } } },
      { text: '  |  ', options: { color: '48635E' } },
      { text: 'RI Food Bank', options: { color: C.paper, hyperlink: { url: 'https://rifoodbank.org/wp-content/uploads/2023/06/Food-Safety-Recall-Process-doc.pdf' } } },
    ], 0.72, 6.94, 6.65, 0.25, { fontSize: 8.5, color: C.aqua });
    addText(slide, '9', 12.52, 7.03, 0.24, 0.2, { fontSize: 8, color: C.aqua, align: 'right' });
    addNotes(slide, 'The proposed next step is a bounded shadow pilot. The goal is to validate the workflow and evidence loop with operators before making impact claims. The public demo and source code are available from this slide.');
  }

  validateBounds();
  await pptx.writeFile({ fileName: output, compression: true });
  console.log(`Wrote ${output}`);
}

build().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
