import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { MockAdapter } from './adapters/mock'
import type { PlatformAdapter } from './adapters/platform'
import { TelegramAdapter } from './adapters/telegram'
import { VkAdapter } from './adapters/vk'
import { App } from './App'
import { PlatformContext } from './hooks/usePlatform'
import { useAuth } from './hooks/useAuth'
import './index.css'

async function bootstrap() {
  let adapter: PlatformAdapter

  const isVk = new URLSearchParams(window.location.search).has('vk_app_id')
  const isDevExplicit =
    Boolean(import.meta.env.DEV) &&
    (import.meta.env.VITE_MOCK_PLATFORM === 'true' ||
      new URLSearchParams(window.location.search).get('dev') === '1')

  if (isDevExplicit) {
    adapter = new MockAdapter()
  } else if (isVk) {
    adapter = new VkAdapter()
  } else {
    adapter = new TelegramAdapter()
  }

  adapter.ready()

  const root = document.getElementById('root')!
  createRoot(root).render(
    <StrictMode>
      <PlatformContext.Provider value={adapter}>
        <BrowserRouter>
          <AppWrapper adapter={adapter} />
        </BrowserRouter>
      </PlatformContext.Provider>
    </StrictMode>,
  )
}

function AppWrapper({ adapter }: { adapter: PlatformAdapter }) {
  const { authState, error, retry } = useAuth(adapter)
  return <App authState={authState} error={error} onRetry={retry} />
}

bootstrap().catch(console.error)
