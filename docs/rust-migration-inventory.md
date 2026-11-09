# Инвентаризация точек входа для переноса на Rust

Снимок исходного Python-кода ветки `codex/rust-rewrite` после коммита `b0e1b85`; актуальные изменения `development` отдельно описаны в плане. Перечень получен статическим разбором декораторов Python: это исходная карта, а не доказательство полного покрытия. Декоратор с префиксом охватывает несколько действий; их нужно разобрать отдельно.

**Статусы:** «частично» — есть отдельные ветви в Rust, но весь исходный обработчик не проверен; «не перенесено» — законченного эквивалента в Rust нет. Для каждой строки перед сменой статуса на «выполнено» добавить точный Rust-обработчик, тесты успеха/ошибок/прав, проверить все вложенные варианты и записать подтверждение в журнал.

## Telegram: зарегистрированные обработчики

| Исходная функция | Регистрация / маршрут | Статус |
| --- | --- | --- |
| [`handle_teacher_choice`](../src/telegram_bot.py#L2332) | `dispatcher.callback_query(F.data.startswith('teacher:'))` | частично |
| [`handle_start`](../src/telegram_bot.py#L2684) | `dispatcher.message(CommandStart())` | частично |
| [`handle_sticker_message`](../src/telegram_bot.py#L2728) | `dispatcher.message(F.sticker)` | частично: сохранение стикера при активном выборе перенесено; нужен полный сквозной тест и сверка обработки ошибок |
| [`handle_donate_command`](../src/telegram_bot.py#L2744) | `dispatcher.message(Command('donate'))` | частично: перенесена команда /donate, инлайн-клавиатура сумм и диалог ввода произвольного количества звезд |
| [`handle_cleandb_command`](../src/telegram_bot.py#L2767) | `dispatcher.message(Command('cleandb'))` | частично: перенесена команда /cleandb для очистки старых записей outbox и processed_updates |
| [`handle_dnremove_command`](../src/telegram_bot.py#L2782) | `dispatcher.message(Command('dnremove'))` | частично: перенесена админская команда /dnremove, вызов refundStarPayment и отметка возврата в БД |
| [`process_pre_checkout`](../src/telegram_bot.py#L2865) | `dispatcher.pre_checkout_query()` | частично: перенесен ответ answerPreCheckoutQuery(ok=true) |
| [`process_successful_payment`](../src/telegram_bot.py#L2869) | `dispatcher.message(F.successful_payment)` | частично: перенесена запись транзакции в star_donations и благодарственное сообщение |
| [`handle_settings_command`](../src/telegram_bot.py#L2914) | `dispatcher.message(Command('settings'))` | частично |
| [`handle_rasp_command`](../src/telegram_bot.py#L2929) | `dispatcher.message(Command('rasp'))` | частично |
| [`handle_cancel_command`](../src/telegram_bot.py#L2943) | `dispatcher.message(Command('cancel'))` | частично: сбрасывает перенесённые режимы; оставшиеся состояния и удаление контекстных сообщений ещё не перенесены |
| [`handle_miniapp_stats_command`](../src/telegram_bot.py#L3132) | `dispatcher.message(Command('app_stats'))` | частично: перенесена агрегация статистики Mini App и генерация отчета для администраторов |
| [`handle_miniapp_stats_command`](../src/telegram_bot.py#L3132) | `dispatcher.message(Command('miniapp_stats'))` | частично: перенесена агрегация статистики Mini App и генерация отчета для администраторов |
| [`handle_app_command`](../src/telegram_bot.py#L3140) | `dispatcher.message(Command('app'))` | частично: кнопка с настроенным адресом перенесена; нужен сквозной тест в Telegram |
| [`handle_bugreport_command`](../src/telegram_bot.py#L3155) | `dispatcher.message(Command('bugreport'))` | частично: перенесён ввод черновика, сохранение в feedback_messages и оповещение администраторов |
| [`handle_feedback_command`](../src/telegram_bot.py#L3167) | `dispatcher.message(Command('feedback', 'support'))` | частично: перенесён ввод черновика, сохранение в feedback_messages и оповещение администраторов |
| [`handle_reply_command`](../src/telegram_bot.py#L3181) | `dispatcher.message(Command('reply'))` | частично: перенесён ответ по ID пользователя/обращения и цитированием сообщения бота администратором |
| [`handle_admin_command`](../src/telegram_bot.py#L3240) | `dispatcher.message(Command('admin'))` | частично: перенесена команда /admin с показом панели полного и ограниченного администратора |
| [`handle_ocr_command`](../src/telegram_bot.py#L3255) | `dispatcher.message(Command('ocr'))` | не перенесено |
| [`handle_group_setup_command`](../src/telegram_bot.py#L3271) | `dispatcher.message(Command('group_setup'))` | частично |
| [`handle_group_set_command`](../src/telegram_bot.py#L3289) | `dispatcher.message(Command('group_set'))` | частично |
| [`handle_group_status_command`](../src/telegram_bot.py#L3321) | `dispatcher.message(Command('group_status'))` | частично |
| [`handle_group_clear_command`](../src/telegram_bot.py#L3336) | `dispatcher.message(Command('group_clear'))` | частично |
| [`handle_menu_start`](../src/telegram_bot.py#L3352) | `dispatcher.callback_query(F.data == 'menu:start')` | частично |
| [`handle_university_subgroup`](../src/telegram_bot.py#L3370) | `dispatcher.callback_query((F.data == 'menu:subgroup') \| F.data.startswith('subgroup:'))` | частично |
| [`handle_menu_settings`](../src/telegram_bot.py#L3410) | `dispatcher.callback_query(F.data == 'menu:settings')` | частично |
| [`handle_start_rasp`](../src/telegram_bot.py#L3429) | `dispatcher.callback_query(F.data == 'start:rasp')` | частично |
| [`handle_schedule_callback`](../src/telegram_bot.py#L3438) | `dispatcher.callback_query(F.data.startswith('schedule:'))` | частично |
| [`handle_report_callback`](../src/telegram_bot.py#L3453) | `dispatcher.callback_query(F.data.startswith('report:'))` | частично: перенесены подтверждение, отмена и отправка обращения |
| [`handle_settings_callback`](../src/telegram_bot.py#L3491) | `dispatcher.callback_query(F.data.startswith('settings:'))` | частично |
| [`handle_help_query`](../src/telegram_bot.py#L3616) | `dispatcher.callback_query(F.data.startswith('help:'))` | частично |
| [`handle_bot_added_to_chat`](../src/telegram_bot.py#L3639) | `dispatcher.my_chat_member()` | частично |
| [`handle_startgroup_command`](../src/telegram_bot.py#L3659) | `dispatcher.message(F.text.func(is_group_setup_command))` | частично |
| [`handle_donate_callback`](../src/telegram_bot.py#L3695) | `dispatcher.callback_query(F.data.startswith('donate:'))` | частично: обработка donate:stars:*, donate:stars:custom и donate:stars:cancel |
| [`handle_admin_callback`](../src/telegram_bot.py#L3734) | `dispatcher.callback_query(F.data.startswith('admin:'))` | частично: перенесены секции monitor/schedule/counters/people/service, статус, статистика, очистка БД, эталоны, перепарсинг, синхронизация счетчиков, список пользователей с пагинацией и массовая рассылка |
| [`handle_editor_toggle`](../src/telegram_bot.py#L4637) | `dispatcher.callback_query(F.data.startswith('editor:toggle:'))` | не перенесено |
| [`handle_admin_ocr_photo`](../src/telegram_bot.py#L4657) | `dispatcher.message(F.photo \| F.document)` | не перенесено |
| [`handle_text_message`](../src/telegram_bot.py#L5065) | `dispatcher.message(F.text)` | частично |
| [`handle_telegram_errors`](../src/telegram_bot.py#L5607) | `dispatcher.errors()` | не перенесено |

## VK: зарегистрированные обработчики

| Исходная функция | Регистрация / маршрут | Статус |
| --- | --- | --- |
| [`all_messages_handler`](../src/vk_bot.py#L2041) | `bot.on.message()`, `bot.on.raw_event()` | частично: перенесён VkEngine с поддержкой message_new, message_event, бесед (peer_id >= 2_000_000_000), команд расписания, звонков, поиска и настроек |

## Админка: маршруты и middleware

| Исходная функция | Регистрация / маршрут | Статус |
| --- | --- | --- |
| [`security_middleware`](../web_configurator/app.py#L216) | `app.middleware('http')` | не перенесено |
| [`unauthorized_handler`](../web_configurator/app.py#L284) | `app.exception_handler(401)` | не перенесено |
| [`lesson_config_error_handler`](../web_configurator/app.py#L289) | `app.exception_handler(LessonConfigReadError)` | не перенесено |
| [`index`](../web_configurator/app.py#L302) | `app.get('/', response_class=HTMLResponse)` | не перенесено |
| [`login_form`](../web_configurator/app.py#L307) | `app.get('/login', response_class=HTMLResponse)` | не перенесено |
| [`login`](../web_configurator/app.py#L332) | `app.post('/login')` | не перенесено |
| [`logout`](../web_configurator/app.py#L364) | `app.post('/logout')` | не перенесено |
| [`api_metrics`](../web_configurator/app.py#L377) | `app.get('/api/metrics')` | не перенесено |
| [`courses_page`](../web_configurator/app.py#L392) | `app.get('/courses', response_class=HTMLResponse)` | не перенесено |
| [`lessons_page`](../web_configurator/app.py#L398) | `app.get('/lessons', response_class=HTMLResponse)` | не перенесено |
| [`sync_lessons`](../web_configurator/app.py#L405) | `app.post('/lessons/sync', response_class=HTMLResponse)` | не перенесено |
| [`save_lessons_json`](../web_configurator/app.py#L422) | `app.post('/lessons/json', response_class=HTMLResponse)` | не перенесено |
| [`add_lesson_group`](../web_configurator/app.py#L459) | `app.post('/lessons/groups')` | не перенесено |
| [`delete_lesson_group`](../web_configurator/app.py#L479) | `app.post('/lessons/groups/delete')` | не перенесено |
| [`upsert_lesson_subject`](../web_configurator/app.py#L493) | `app.post('/lessons/subjects')` | не перенесено |
| [`upsert_lesson_subjects_bulk`](../web_configurator/app.py#L537) | `app.post('/lessons/subjects/bulk')` | не перенесено |
| [`delete_lesson_subject`](../web_configurator/app.py#L558) | `app.post('/lessons/subjects/delete')` | не перенесено |
| [`web_users_page`](../web_configurator/app.py#L576) | `app.get('/web-users', response_class=HTMLResponse)` | не перенесено |
| [`save_web_user`](../web_configurator/app.py#L581) | `app.post('/web-users')` | не перенесено |
| [`delete_web_user`](../web_configurator/app.py#L592) | `app.post('/web-users/delete')` | не перенесено |
| [`control_page`](../web_configurator/app.py#L601) | `app.get('/control', response_class=HTMLResponse)` | не перенесено |
| [`control_broadcast`](../web_configurator/app.py#L606) | `app.post('/control/broadcast', response_class=HTMLResponse)` | не перенесено |
| [`control_test_broadcast`](../web_configurator/app.py#L622) | `app.post('/control/test', response_class=HTMLResponse)` | не перенесено |
| [`control_refresh`](../web_configurator/app.py#L632) | `app.post('/control/refresh', response_class=HTMLResponse)` | не перенесено |
| [`control_baseline`](../web_configurator/app.py#L638) | `app.post('/control/baseline', response_class=HTMLResponse)` | не перенесено |
| [`gemini_page`](../web_configurator/app.py#L644) | `app.get('/gemini', response_class=HTMLResponse)` | не перенесено |

## Mini App API: маршруты

| Исходная функция | Регистрация / маршрут | Статус |
| --- | --- | --- |
| [`health_check`](../miniapp_api/app.py#L104) | `app.get('/api/health')` | не перенесено |
| [`health_check`](../miniapp_api/app.py#L104) | `app.get('/health')` | не перенесено |
| [`auth_telegram`](../miniapp_api/app.py#L130) | `app.post('/api/miniapp/v1/auth/telegram', response_model=AuthResponse)` | не перенесено |
| [`auth_vk`](../miniapp_api/app.py#L162) | `app.post('/api/miniapp/v1/auth/vk', response_model=AuthResponse)` | не перенесено |
| [`logout`](../miniapp_api/app.py#L229) | `app.post('/api/miniapp/v1/auth/logout')` | не перенесено |
| [`get_me`](../miniapp_api/app.py#L296) | `app.get('/api/miniapp/v1/me', response_model=ProfileResponse)` | не перенесено |
| [`update_preferences`](../miniapp_api/app.py#L304) | `app.patch('/api/miniapp/v1/me/preferences', response_model=ProfileResponse)` | не перенесено |
| [`search_groups`](../miniapp_api/app.py#L328) | `app.get('/api/miniapp/v1/groups', response_model=list[GroupItemDto])` | не перенесено |
| [`add_subscription`](../miniapp_api/app.py#L394) | `app.post('/api/miniapp/v1/me/subscriptions', response_model=SubscriptionDto)` | не перенесено |
| [`update_subscription`](../miniapp_api/app.py#L434) | `app.patch('/api/miniapp/v1/me/subscriptions/{sub_id}', response_model=SubscriptionDto)` | не перенесено |
| [`delete_subscription`](../miniapp_api/app.py#L467) | `app.delete('/api/miniapp/v1/me/subscriptions/{sub_id}')` | не перенесено |
| [`create_link_request`](../miniapp_api/app.py#L483) | `app.post('/api/miniapp/v1/account-links', response_model=CreateLinkRequestResponse)` | не перенесено |
| [`confirm_link_request`](../miniapp_api/app.py#L496) | `app.post('/api/miniapp/v1/account-links/confirm')` | не перенесено |
| [`unlink_identity`](../miniapp_api/app.py#L519) | `app.delete('/api/miniapp/v1/me/identities/{platform}')` | не перенесено |
| [`get_schedule`](../miniapp_api/app.py#L636) | `app.get('/api/miniapp/v1/schedule', response_model=ScheduleResponse)` | не перенесено |
| [`get_history`](../miniapp_api/app.py#L674) | `app.get('/api/miniapp/v1/history', response_model=HistoryResponse)` | не перенесено |
| [`get_user_avatar`](../miniapp_api/app.py#L759) | `app.get('/api/miniapp/v1/avatar/{platform}/{user_id}')` | не перенесено |
| [`telemetry_session`](../miniapp_api/app.py#L907) | `app.post('/api/miniapp/v1/telemetry/session')` | не перенесено |
| [`telemetry_ping`](../miniapp_api/app.py#L924) | `app.post('/api/miniapp/v1/telemetry/ping')` | не перенесено |
| [`telemetry_event`](../miniapp_api/app.py#L937) | `app.post('/api/miniapp/v1/telemetry/event')` | не перенесено |

## Планировщик: зарегистрированные задания

Все строки ниже — **не выполнено** как полные сценарии; отдельные функции синхронизации/очереди на Rust имеются. Проверять таймзону, `coalesce`, `max_instances`, условия включения, повтор после перезапуска и обработку broker fallback по `src/scheduler.py`.

| Задание Python | Триггер / условие | Статус |
| --- | --- | --- |
| `save_daily_baseline` | ежедневно 00:00 | частично: реализован метод `save_daily_baselines` в `scheduler.rs` |
| `save_daily_baseline_fallback` | ежедневно 05:00 | частично: поддерживается через `save_daily_baselines` |
| `sync_current_snapshot` | каждый час на 10-й минуте; дополнительно 09–13 в 40 минут | частично: реализован метод `sync_active_sources` с `apply_and_enqueue` в `scheduler.rs` |
| `enqueue_or_run_auto_daily_lesson_counter` | 23:20 и 23:50 за текущий день; 01:00 и 05:00 за предыдущий, при включённом счётчике | не выполнено |
| `send_admin_backup` | интервал из конфигурации, при включённых backup | не выполнено |
| `enqueue_or_run_db_cleanup` | понедельник 04:30 | частично: реализован метод `cleanup_old_records` в `scheduler.rs` |
| `run_system_health_check` | каждые 5 минут | не выполнено |
| `refresh_group_catalog` | настроенный интервал дней, при наличии каталога | не выполнено |
| `retry_group_catalog_departments` | каждые 30 минут, при наличии каталога | не выполнено |
| `promote_pending_teacher_subscribers` | каждые 30 минут, при наличии поискового каталога | не выполнено |

## Telegram: ветви внутри широких обработчиков

Наличие отдельной Rust-ветви не означает полного совпадения обработки ошибок, очистки сообщений или интеграции с реальным ботом. Поэтому даже перенесённые ниже действия обозначены как **частично** до полного сквозного теста.

| Префикс и действие | Поведение | Статус |
| --- | --- | --- |
| `schedule:today`, `schedule:tomorrow`, `schedule:day_after` | Показать расписание выбранного дня | частично |
| `schedule:week:<date>:<variant>` | Выбрать актуальную неделю, числитель или знаменатель | частично |
| `schedule:bells` | Показать расписание звонков | частично |
| `schedule:find` | Поиск группы, преподавателя или кабинета | частично |
| `settings:toggle_notifications` | Включить или отключить уведомления | частично |
| `settings:audience_setup`, `settings:audience_clear` | Выбрать или отвязать кабинет преподавателя | частично |
| `settings:clear_group` | Очистить подписку | частично |
| `settings:help`, `help:*` | Разделы справки и возврат | частично |
| `settings:personalization`, `settings:pers_set_sticker`, `settings:pers_clear_sticker`, `settings:pers_preview_sticker` | Выбор, удаление и предпросмотр стикера | частично; ошибки предпросмотра ещё не доведены |
| `settings:about` | Информация о проекте и навигация настроек | частично: перенесён экран, требуется сквозной тест в Telegram |
| `settings:lesson_counters` | Статистика занятий | частично: перенесён экран счетчиков занятий |
| `settings:bugreport`, `settings:feedback`, `settings:support`, `report:*` | Обращение, черновик, отмена и отправка | частично: перенесён ввод черновика, подтверждение, отправка и ответы админа; требуется сквозной тест в Telegram |
| `donate:*`, `process_pre_checkout`, `process_successful_payment` | Выбор суммы, платёж и подтверждение | частично: перенесены выбор суммы, инвойс, pre_checkout и successful_payment |
| `admin:counter_sync*`, `admin:cleandb`, `admin:baseline`, `admin:refresh` | Пересчёт, очистка, базовый снимок и обновление | частично: перенесены обработчики действий администратора |
| `admin:broadcast*`, `admin:download_db`, `admin:download_counters` | Рассылка и выдача файлов администратору | частично: перенесены ввод текста, фильтрация по платформе и аудитории и запуск рассылки |
| `admin:import_lessons*`, `admin:ocr_*` | Импорт занятий и OCR-диалоги | не перенесено |
| `admin:lesson_*`, `editor:toggle:*` | Добавление, редактирование и удаление занятий, права редактора | не перенесено |
| `admin:status`, `admin:daily_errors`, `admin:gemini_status`, `admin:users*`, `admin:editors` | Мониторинг, ошибки, Gemini, пользователи и редакторы | частично: перенесены статус, ошибки за день, список пользователей с пагинацией |
| `admin:last_change`, `admin:group_info`, `admin:course_stats`, `admin:miniapp_stats`, `admin:back`, `admin:close` | История, сведения и статистика, навигация админки | частично: перенесены статистика Mini App, разделы sec:* и навигация |

Здесь перечислены основные явно обнаруженные ветви; вложенные фильтры, текстовые состояния и варианты внутри `admin:*` и VK по-прежнему нужно сверять с полным исходным кодом и тестами.

## Запуск, брокер, OCR и фоновые сервисы

| Исходный компонент | Назначение | Статус |
| --- | --- | --- |
| `src/main.py:main` / `shutdown` | Инициализация, жизненный цикл и корректное завершение всех подсистем | не перенесено |
| `start_telegram_polling`, `start_vk_polling`, `run_forever` | Запуск и восстановление приёмников после ошибок | не перенесено; есть отдельные основы long poll |
| `telegram_polling_probe`, VK liveness, `log_memory` | Мониторинг приёма и памяти процесса | не перенесено |
| `warm_up_ocr_and_notify`, `should_notify_ocr_ready` | Прогрев OCR, ограничения повторных сообщений администратору | не перенесено |
| `RabbitMQBroker`, `OutboundMessage` | Публикация/потребление уведомлений и состояние брокера | не перенесено |
| `OutboxBroker` | Транспорт задач outbox через RabbitMQ | не перенесено; есть локальное ядро SQLite outbox |
| `DatabaseCleanupJobBroker`, `DatabaseCleanupJob` | Надёжное выполнение обслуживания БД | не перенесено |
| `AutoDailyLessonCounterJobBroker`, `AutoDailyLessonCounterJob` | Задания ежедневного подсчёта занятий | не перенесено |
| `ArchiveBackfill` | Возобновляемое восстановление архивных дней из снимков | не перенесено; есть отдельная запись архивных проекций |
| `OcrScheduleImporter`, черновики и предпросмотр | Диалог импорта, выбор группы, подтверждение и статус | не перенесено |
| `GeminiOcrEngine`, `OcrScheduleParser`, `compress_image_for_ocr` | Доступ к Gemini, подготовка изображения, распознавание и нормализация | не перенесено |
| `merge_ocr_days`, `build_ocr_engine` | Объединение распознанного расписания, конфигурация движка | не перенесено |
| `AdminErrorReporter`, `AdminLogHandler` | Агрегация ошибок и сообщения администраторам | не перенесено |
| `src/course_stats.py`, `src/lesson_counters.py` | Статистика курсов, подсчёт и сохранение занятий | не перенесено целиком; отдельные функции расписания уже есть |
| `src/miniapp_calendar.py`, `miniapp_diff.py`, `miniapp_stats.py` | История, календарь и статистика Mini App | не перенесено целиком |

## Mini App: клиентские части

В исходном проекте клиент — React/TypeScript/Vite; **все строки ещё не перенесены на Rust/Wasm**. HTML/CSS и ресурсы являются частью интерфейса и должны сохраняться по поведению и внешнему виду. Перед выбором Rust-фреймворка снять размеры сборки и проверить Telegram/VK WebView.

| Компонент / файлы | Сценарий для переноса | Статус |
| --- | --- | --- |
| `main.tsx`, `App.tsx`, `App.css`, `index.css` | Инициализация, маршрутизация, общие стили и состояния | не перенесено |
| `pages/Schedule.tsx`, `components/DayView.tsx`, `LessonCard.tsx` | Расписание, переходы между днями, карточки и ошибки | не перенесено |
| `pages/History.tsx` | Просмотр истории и навигация по датам | не перенесено |
| `pages/Profile.tsx` | Профиль, подписки, настройки, связанные сценарии | не перенесено |
| `components/BottomNav.tsx`, `BrowserBlockScreen.tsx`, `LoadingSkeleton.tsx`, `ErrorState.tsx`, `PlatformIcons.tsx` | Навигация, ограничения запуска, состояния загрузки/ошибок, иконки | не перенесено |
| `adapters/telegram.ts`, `vk.ts`, `platform.ts`, `mock.ts` | Интеграция Telegram/VK, определение платформы, тестовые данные | не перенесено |
| `api/client.ts`, `auth.ts`, `groups.ts`, `history.ts`, `profile.ts`, `schedule.ts`, `subscriptions.ts`, `telemetry.ts` | API-клиент и обработка ответов/ошибок | не перенесено |
| `hooks/useAuth.ts`, `usePlatform.ts`, `useScale.ts`, `useTelemetry.ts`, `useTheme.ts` | Состояние авторизации, платформа, масштаб, телеметрия и тема | не перенесено |
| `lib/clipboard.ts`, `utils.ts`, `assets/` | Буфер обмена, общие функции и статические материалы | не перенесено |
| `src/__tests__/` и scripts в `package.json` | Тесты экранов, bridge, хуков и build/typecheck | Rust/Wasm-эквиваленты не подготовлены |

## Миграции и служебные сценарии

| Источник | Назначение | Статус |
| --- | --- | --- |
| `migrations/versions/20260418_0001_initial_schema.py` | Начальная схема | новые Rust-базы создаются из `schema.sql`; перенос старых версий не реализован |
| `20261029_0002_delivery_outbox.py` | Таблицы устойчивой доставки | схема новой БД есть; переход старой версии не перенесён |
| `20261029_0003_university_subgroup.py` | Поле подгруппы | схема новой БД есть; переход старой версии не перенесён |
| `20261105_0004_schedule_day_archive.py` | Архив дней и опорные состояния | схема и часть работы с архивом есть; миграция старой БД не перенесена |
| `20261105_0005_archive_backfill.py`, `20261105_0006_archive_snapshot_markers.py` | Контроль backfill и маркеры проекций | схема есть; перенос исполнения backfill и миграций не завершён |
| `20261105_0007_accounts_and_subscriptions.py` | Аккаунты и подписки | часть операций и схема есть; переход старой БД не реализован |
| `scripts/deploy_production.py`, `deploy_request.py`, `deploy_ssh.py` | Деплой и взаимодействие с сервером | Rust-эквиваленты и проверенный сценарий переключения отсутствуют |
| `scripts/generate_release_notes.py` | Генерация заметок о релизе | перенос или готовая совместимая замена не проверены |
| `scripts/import_schedule.py`, `import_schedule_photos.py` | Импорт расписания и фотографий | перенос и проверка импорта не завершены |
| `scripts/seed_pending_groups.py` | Начальное заполнение ожидающих групп | перенос или готовая замена не проверены |

## Что обязательно дополнить при переносе

- `handle_schedule_callback`, `handle_settings_callback`, `handle_admin_callback`, `handle_text_message` и VK `all_messages_handler` содержат множество ветвей. Для каждого значения callback/кнопки, состояния и команды требуется отдельная строка в дочерней таблице; отметка всего обработчика без этой сверки недопустима.
- Добавить таблицы по `src/main.py`, brokers/consumers, OCR, `scripts/`, миграциям, вложениям и всем экранам `miniapp_frontend/`; сопоставить с тестами и реальным запуском.
- Сверить маршруты с новой `origin/development`, включая тесты Mini App, затем обновлять номера строк и статусы после каждого законченного изменения. Ни одна полная фаза не закрыта.
