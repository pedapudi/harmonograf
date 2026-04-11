import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

// Apply persisted theme before React mounts so the first paint is correctly
// themed. The store also re-applies on rehydration but this avoids a flash.
import { bootstrapTheme } from './theme/store';
bootstrapTheme();

// MD3 typography token registration. @material/web components consume the same
// CSS custom properties we set in src/theme/themes.ts; importing the typography
// module ensures default font weights are set.
import '@material/web/typography/md-typescale-styles.js';

import './index.css';
import App from './App.tsx';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
