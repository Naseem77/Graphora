import React from "react";
import {
  AbsoluteFill,
  Easing,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

const BG = "#0d1117";
const PANEL = "#161b22";
const BORDER = "#30363d";
const TEXT = "#e6edf3";
const DIM = "#8b949e";
const ACCENT = "#f78166";
const GREEN = "#3fb950";
const BLUE = "#58a6ff";
const PURPLE = "#bc8cff";

const ease = Easing.bezier(0.16, 1, 0.3, 1);

const appear = (frame: number, from: number, dur = 20) =>
  interpolate(frame, [from, from + dur], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: ease,
  });

const FILES = [
  { name: "auth.py", color: BLUE },
  { name: "api.rs", color: ACCENT },
  { name: "db.go", color: GREEN },
  { name: "app.tsx", color: PURPLE },
];

const FileStack: React.FC<{ frame: number }> = ({ frame }) => (
  <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
    {FILES.map((f, i) => {
      const p = appear(frame, 10 + i * 8);
      return (
        <div
          key={f.name}
          style={{
            opacity: p,
            translate: `${(1 - p) * -60}px 0px`,
            display: "flex",
            alignItems: "center",
            gap: 12,
            background: PANEL,
            border: `1px solid ${BORDER}`,
            borderRadius: 10,
            padding: "12px 20px",
            width: 190,
          }}
        >
          <div
            style={{
              width: 12,
              height: 12,
              borderRadius: 4,
              background: f.color,
            }}
          />
          <span
            style={{
              color: TEXT,
              fontSize: 26,
              fontFamily: "ui-monospace, Menlo, monospace",
            }}
          >
            {f.name}
          </span>
        </div>
      );
    })}
  </div>
);

type N = { x: number; y: number; label: string; c: string; risk?: boolean };
const NODES: N[] = [
  { x: 170, y: 60, label: "handler()", c: BLUE },
  { x: 60, y: 160, label: "auth()", c: ACCENT, risk: true },
  { x: 280, y: 160, label: "query()", c: GREEN },
  { x: 110, y: 270, label: "test_auth", c: PURPLE },
  { x: 250, y: 270, label: "save()", c: BLUE },
];
const EDGES: [number, number][] = [
  [0, 1],
  [0, 2],
  [1, 3],
  [2, 4],
  [1, 2],
];

const Graph: React.FC<{ frame: number; start: number }> = ({
  frame,
  start,
}) => {
  const pulse = Math.sin(frame / 9) * 0.5 + 0.5;
  return (
    <svg width={360} height={340} style={{ overflow: "visible" }}>
      {EDGES.map(([a, b], i) => {
        const p = appear(frame, start + 10 + i * 6, 16);
        const n1 = NODES[a];
        const n2 = NODES[b];
        return (
          <line
            key={i}
            x1={n1.x}
            y1={n1.y}
            x2={n1.x + (n2.x - n1.x) * p}
            y2={n1.y + (n2.y - n1.y) * p}
            stroke={BORDER}
            strokeWidth={3}
            opacity={p}
          />
        );
      })}
      {NODES.map((n, i) => {
        const p = appear(frame, start + i * 6, 18);
        const riskGlow = n.risk ? 10 + pulse * 14 : 0;
        return (
          <g key={i} opacity={p} transform={`translate(${n.x}, ${n.y})`}>
            <circle
              r={26 * p}
              fill={PANEL}
              stroke={n.c}
              strokeWidth={3.5}
              style={{
                filter: n.risk
                  ? `drop-shadow(0 0 ${riskGlow}px ${ACCENT})`
                  : undefined,
              }}
            />
            <text
              y={48}
              textAnchor="middle"
              fill={n.risk ? ACCENT : DIM}
              fontSize={22}
              fontFamily="ui-monospace, Menlo, monospace"
            >
              {n.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
};

const OUTPUTS = [
  { label: "blast radius", icon: "◎", c: BLUE },
  { label: "diff review + risk", icon: "⚠", c: ACCENT },
  { label: "MCP tools for agents", icon: "⌘", c: GREEN },
];

const Outputs: React.FC<{ frame: number; start: number }> = ({
  frame,
  start,
}) => (
  <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
    {OUTPUTS.map((o, i) => {
      const p = appear(frame, start + i * 10);
      return (
        <div
          key={o.label}
          style={{
            opacity: p,
            translate: `${(1 - p) * 60}px 0px`,
            display: "flex",
            alignItems: "center",
            gap: 14,
            background: PANEL,
            border: `1px solid ${BORDER}`,
            borderLeft: `4px solid ${o.c}`,
            borderRadius: 10,
            padding: "14px 22px",
            width: 320,
          }}
        >
          <span style={{ fontSize: 28, color: o.c }}>{o.icon}</span>
          <span style={{ color: TEXT, fontSize: 26, fontWeight: 600 }}>
            {o.label}
          </span>
        </div>
      );
    })}
  </div>
);

const Arrow: React.FC<{ frame: number; start: number }> = ({
  frame,
  start,
}) => {
  const p = appear(frame, start, 16);
  return (
    <div
      style={{
        opacity: p,
        translate: `${(1 - p) * -20}px 0px`,
        color: DIM,
        fontSize: 44,
        padding: "0 8px",
      }}
    >
      →
    </div>
  );
};

const Counter: React.FC<{ frame: number; start: number }> = ({
  frame,
  start,
}) => {
  const p = appear(frame, start, 24);
  const tokens = Math.round(
    interpolate(frame, [start, start + 40], [22160, 1237], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
      easing: Easing.bezier(0.4, 0, 0.2, 1),
    }),
  );
  const done = frame > start + 40;
  return (
    <div
      style={{
        opacity: p,
        display: "flex",
        alignItems: "baseline",
        gap: 18,
        justifyContent: "center",
      }}
    >
      <span
        style={{
          fontFamily: "ui-monospace, Menlo, monospace",
          fontSize: 54,
          fontWeight: 700,
          color: done ? GREEN : TEXT,
        }}
      >
        {tokens.toLocaleString("en-US")} tokens
      </span>
      <span style={{ fontSize: 34, color: DIM }}>
        {done ? "· 94% less context · $0 · no LLM" : "of repo context…"}
      </span>
    </div>
  );
};

export const Banner: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const titleP = appear(frame, 0, 18);
  const graphStart = 1.4 * fps;
  const outStart = 3.2 * fps;
  const counterStart = 4.6 * fps;

  return (
    <AbsoluteFill
      style={{
        background: BG,
        fontFamily:
          "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif",
        justifyContent: "center",
        alignItems: "center",
      }}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 14,
          width: "100%",
        }}
      >
        <div
          style={{
            opacity: titleP,
            translate: `0px ${(1 - titleP) * -20}px`,
            fontSize: 40,
            fontWeight: 700,
            color: TEXT,
            letterSpacing: 0.5,
          }}
        >
          Graphora{" "}
          <span style={{ color: DIM, fontWeight: 400 }}>
            · your codebase as a deterministic knowledge graph
          </span>
        </div>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <FileStack frame={frame} />
          <Arrow frame={frame} start={graphStart - 8} />
          <Graph frame={frame} start={graphStart} />
          <Arrow frame={frame} start={outStart - 8} />
          <Outputs frame={frame} start={outStart} />
        </div>

        <Counter frame={frame} start={counterStart} />
      </div>
    </AbsoluteFill>
  );
};
