import React from "react";
import {
  AbsoluteFill,
  Img,
  staticFile,
  interpolate,
  Easing,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

export type KenBurnsProps = {
  src: string; // filename resolved against --public-dir (the run folder)
  zoomFrom: number;
  zoomTo: number;
  panX: number; // fraction of the frame travelled horizontally
  panY: number;
  glow: boolean;
  grain: boolean;
  vignette: boolean;
  durationInFrames: number;
  fps: number;
  width: number;
  height: number;
};

export const kenBurnsDefaults: KenBurnsProps = {
  src: "key0.png",
  zoomFrom: 1.0,
  zoomTo: 1.14,
  panX: 0.05,
  panY: 0.05,
  glow: true,
  grain: true,
  vignette: true,
  durationInFrames: 120,
  fps: 30,
  width: 1080,
  height: 1920,
};

// The animated upgrade over ffmpeg's linear zoompan:
//   • eased zoom + diagonal pan (no robotic constant-velocity drift)
//   • a slow light glow drifting across the frame (motion graphic)
//   • a soft vignette + subtle film grain for a cinematic finish
// The keyframe is oversized so the pan/zoom never exposes an edge.
export const KenBurns: React.FC<KenBurnsProps> = (props) => {
  const {src, zoomFrom, zoomTo, panX, panY, glow, grain, vignette} = props;
  const frame = useCurrentFrame();
  const {durationInFrames, width, height} = useVideoConfig();

  const span = Math.max(1, durationInFrames - 1);
  const linear = frame / span; // 0 -> 1
  const t = interpolate(linear, [0, 1], [0, 1], {
    easing: Easing.inOut(Easing.ease),
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // OVERSCAN: render the image bigger than the frame so a 14% zoom + 5% pan
  // always stays covered.
  const OVER = 1.25;
  const scale = interpolate(t, [0, 1], [zoomFrom, zoomTo]) * OVER;
  const tx = interpolate(t, [0, 1], [0, panX * width]);
  const ty = interpolate(t, [0, 1], [0, panY * height]);

  // glow path across the frame
  const gx = interpolate(t, [0, 1], [width * 0.25, width * 0.75]);
  const gy = interpolate(t, [0, 1], [height * 0.65, height * 0.35]);

  return (
    <AbsoluteFill style={{backgroundColor: "#000", overflow: "hidden"}}>
      <AbsoluteFill
        style={{
          transform: `translate(${tx}px, ${ty}px) scale(${scale})`,
          transformOrigin: "center center",
        }}
      >
        <Img
          src={isUrl(src) ? src : staticFile(src)}
          style={{width: "100%", height: "100%", objectFit: "cover"}}
        />
      </AbsoluteFill>

      {glow ? (
        <AbsoluteFill
          style={{
            background: `radial-gradient(420px 420px at ${gx}px ${gy}px, rgba(255,238,200,0.18), rgba(255,238,200,0) 70%)`,
            mixBlendMode: "screen",
          }}
        />
      ) : null}

      {vignette ? (
        <AbsoluteFill
          style={{
            background:
              "radial-gradient(120% 80% at 50% 45%, rgba(0,0,0,0) 55%, rgba(0,0,0,0.55) 100%)",
          }}
        />
      ) : null}

      {grain ? <Grain seed={frame} opacity={0.06} /> : null}
    </AbsoluteFill>
  );
};

const isUrl = (s: string) => /^(https?:|file:|data:)/.test(s);

// Animated film grain via an SVG turbulence whose seed advances each frame.
const Grain: React.FC<{seed: number; opacity: number}> = ({seed, opacity}) => (
  <AbsoluteFill style={{opacity, mixBlendMode: "overlay"}}>
    <svg width="100%" height="100%">
      <filter id="grain">
        <feTurbulence
          type="fractalNoise"
          baseFrequency="0.9"
          numOctaves={2}
          seed={seed % 100}
          stitchTiles="stitch"
        />
        <feColorMatrix type="saturate" values="0" />
      </filter>
      <rect width="100%" height="100%" filter="url(#grain)" />
    </svg>
  </AbsoluteFill>
);
