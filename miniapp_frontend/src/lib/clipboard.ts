import bridge from '@vkontakte/vk-bridge'

/**
 * Надежное копирование текста в буфер обмена для Telegram, VK Mini Apps и обычных браузеров.
 */
export async function copyTextToClipboard(text: string): Promise<boolean> {
  // 1. Попытка через VK Bridge (если приложение открыто во ВКонтакте)
  try {
    if (bridge && typeof bridge.send === 'function') {
      const res = await bridge.send('VKWebAppCopyText', { text })
      if (res && res.result) {
        return true
      }
    }
  } catch {
    // Вне VK или метод отклонен клиентом
  }

  // 2. Попытка через стандартный Clipboard API браузера
  if (typeof navigator !== 'undefined' && navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Заблокировано политикой безопасности или iframe
    }
  }

  // 3. Fallback через временный скрытый элемент textarea и execCommand
  if (typeof document !== 'undefined') {
    try {
      const textarea = document.createElement('textarea')
      textarea.value = text
      textarea.setAttribute('readonly', '')
      textarea.style.position = 'fixed'
      textarea.style.left = '-9999px'
      textarea.style.top = '-9999px'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.focus()
      textarea.select()
      const successful = document.execCommand('copy')
      document.body.removeChild(textarea)
      if (successful) {
        return true
      }
    } catch {
      // Игнорируем
    }
  }

  return false
}
