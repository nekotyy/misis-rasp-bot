import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ErrorState } from '../components/ErrorState'

describe('ErrorState', () => {
  it('отображает состояние ошибки сети', () => {
    render(<ErrorState type="network" />)
    expect(screen.getByText('Нет соединения')).toBeInTheDocument()
    expect(screen.getByText(/интернет-соединение/i)).toBeInTheDocument()
  })

  it('отображает состояние ошибки авторизации', () => {
    render(<ErrorState type="auth" />)
    expect(screen.getByText('Ошибка авторизации')).toBeInTheDocument()
  })

  it('отображает состояние отсутствия подписок', () => {
    render(<ErrorState type="no_subscription" />)
    expect(screen.getByText('Нет подписок')).toBeInTheDocument()
  })

  it('вызывает onRetry при клике на кнопку повторить', () => {
    const handleRetry = vi.fn()
    render(<ErrorState type="server" onRetry={handleRetry} />)
    const button = screen.getByRole('button', { name: /повторить/i })
    expect(button).toBeInTheDocument()
    fireEvent.click(button)
    expect(handleRetry).toHaveBeenCalledTimes(1)
  })

  it('отображает переданный кастомный message', () => {
    render(<ErrorState type="server" message="Кастомный сбой" />)
    expect(screen.getByText('Кастомный сбой')).toBeInTheDocument()
  })
})
