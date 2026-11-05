import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { BrowserBlockScreen } from '../components/BrowserBlockScreen'

describe('BrowserBlockScreen', () => {
  it('отображает заголовок блокировки входа через браузер', () => {
    render(<BrowserBlockScreen />)
    expect(screen.getByText('Вход через браузер запрещён')).toBeDefined()
    expect(screen.getByText('Только для мессенджеров')).toBeDefined()
  })

  it('отображает кнопки перехода в Telegram и ВКонтакте', () => {
    render(
      <BrowserBlockScreen
        telegramUrl="https://t.me/test_bot"
        vkUrl="https://vk.com/test_app"
      />,
    )
    const tgLink = screen.getByText('Открыть в Telegram').closest('a')
    expect(tgLink).toBeDefined()
    expect(tgLink?.getAttribute('href')).toBe('https://t.me/test_bot')

    const vkLink = screen.getByText('Открыть ВКонтакте').closest('a')
    expect(vkLink).toBeDefined()
    expect(vkLink?.getAttribute('href')).toBe('https://vk.com/test_app')
  })

  it('использует дефолтную ссылку на группу ВКонтакте', () => {
    render(<BrowserBlockScreen />)
    const vkLink = screen.getByText('Открыть ВКонтакте').closest('a')
    expect(vkLink?.getAttribute('href')).toBe('https://vk.ru/nekotyopk')
  })

  it('использует дефолтную ссылку на Telegram бота @raspnekotybot', () => {
    render(<BrowserBlockScreen />)
    const tgLink = screen.getByText('Открыть в Telegram').closest('a')
    expect(tgLink?.getAttribute('href')).toBe('https://t.me/raspnekotybot')
  })

  it('отображает кастомное сообщение если оно передано', () => {
    render(<BrowserBlockScreen message="Прямой доступ закрыт!" />)
    expect(screen.getByText('Прямой доступ закрыт!')).toBeDefined()
  })

  it('отображает информационные возможности и сноску безопасности', () => {
    render(<BrowserBlockScreen />)
    expect(screen.getByText('Расписание групп, преподавателей и аудиторий')).toBeDefined()
    expect(screen.getByText('Мгновенные push-уведомления об отменах и заменах')).toBeDefined()
    expect(screen.getByText('Доступ защищён сквозной HMAC-верификацией')).toBeDefined()
  })
})
