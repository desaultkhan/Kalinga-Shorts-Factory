import React from "react";
import {Composition} from "remotion";
import {KenBurns, kenBurnsDefaults, KenBurnsProps} from "./KenBurns";

// One composition, sized from --props by the Python side (kenburns.py drives
// KenBurns — the optional richer still-segment animator).
export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="KenBurns"
        component={KenBurns}
        durationInFrames={kenBurnsDefaults.durationInFrames}
        fps={kenBurnsDefaults.fps}
        width={kenBurnsDefaults.width}
        height={kenBurnsDefaults.height}
        defaultProps={kenBurnsDefaults}
        calculateMetadata={({props}: {props: KenBurnsProps}) => ({
          durationInFrames: Math.max(1, Math.round(props.durationInFrames)),
          fps: props.fps,
          width: props.width,
          height: props.height,
        })}
      />
    </>
  );
};
