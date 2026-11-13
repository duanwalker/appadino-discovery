import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { enableOperatorHeader } from "./operatorHeader.ts";
import "./styles.css";

// Only the Vite dev server sends the local operator header; a production build
// (import.meta.env.DEV === false) never does.
enableOperatorHeader(import.meta.env.DEV);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
