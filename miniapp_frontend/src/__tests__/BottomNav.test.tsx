import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { BottomNav } from '../components/BottomNav'

describe('BottomNav', () => {
  it('рендерит все вкладки навигации', () => {
    render(
      <MemoryRouter initialEntries={['/schedule']}>
        <BottomNav />
      </MemoryRouter>,
    )

    expect(screen.getByText('Расписание')).toBeInTheDocument()
    expect(screen.getByText('История')).toBeInTheDocument()
    expect(screen.getByText('Профиль')).toBeInTheDocument()
  })

  it('применяет отступ safe area bottom', () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/schedule']}>
        <BottomNav safeAreaBottom={24} />
      </MemoryRouter>,
    )

    const nav = container.querySelector('nav')
    expect(nav).toBeInTheDocument()
    expect(nav?.style.paddingBottom).toContain('24px')
  })
})
