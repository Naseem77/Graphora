import "./index.css";
import { Composition } from "remotion";
import { Banner } from "./Banner";

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="Banner"
      component={Banner}
      durationInFrames={240}
      fps={30}
      width={1280}
      height={560}
    />
  );
};
