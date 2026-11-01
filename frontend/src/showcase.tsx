import { createRoot } from 'react-dom/client';
import App from './App';
import { OrbitEffects } from './OrbitEffects';
import './styles.css';
import './orbit-chat.css';
import './orbit-effects.css';

document.documentElement.dataset.theme = 'orbit';
createRoot(document.getElementById('root')!).render(
  <>
    <OrbitEffects />
    <App appearance="orbit" />
  </>
);
