
list_error = ["Кнопка находится в разработке.",
                "Эта функция сейчас не работает. Попробуйте позже.",
                "Эта кнопка временно недоступна.",
                "Функция находится в разработке и пока не активна.",
                "Данная функция временно не доступна.",
                "Доступ к этой кнопке в данный момент отключён. Пожалуйста, проверьте позже.",]

list_admin = ["Эта команда только для Администратора.", "Доступ ограничен.", "У вас нет доступа к этой команде."]

list_about = ["Этот бот воплощает в себе мои усилия по превращению цифрового хаоса в более упорядоченное место. С каждой строчкой кода я стремлюсь принести пользу и порядок. Мои проекты, по ссылкам ниже.",
                "... о мире, где машины стремятся к господству, он выбрал судьбу героя, создавая ботов, как первый шаг к спасению человечества через код и умные алгоритмы",]

list_sorry = ["К сожалению, эта функция сейчас не работает.",
              "Эта кнопка временно недоступна.", "Доступ к этой кнопке в данный момент отключён."]

joke_text = []

list_start_text = ['Привет.\n',
                            'Я экспериментальный Telegram bot, model Т-3. ',
                            'Создан для проверки и отладки навыков главного разработчика. Реализую различные команды, методы и функции.\n\n',
                            'Весь основной функционал находится на Главной панели. Полный список команд - /help.\n\n',
                            'Наслаждайся, буду рад помочь.\n',
                            'Пока не обрету AGI.\n',
                            'А там посмотрим ...']

list_advice_weather = [
                  '<blockquote>1. Сначала, выберети ПЕРИОД прогноза:\n - текущий прогноз\n - прогноз на три дня\n\n',
                  '2. Далее, выберите СПОСОБ определения прогноза:\n - по названию населенного пункта\n - по вашей текущей геопозиции </blockquote>']


list_advice_word = [
                    '<blockquote>Режим для прохождения различных викторин на угадывание слов.\n\n',
                    'Если захотите выйти из диалога, отправьте точку</blockquote>']

list_advice_pizza = [
                    '<blockquote>Режим для расчета стоимости квадратного сантиметра пиццы.\n\n',
                    'Если захотите выйти из диалога, отправьте точку</blockquote>']

list_advice_currency = [
                    '<blockquote>Режим для расчета валютной конверсии.\n\n',
                    'Если захотите выйти из диалога, отправьте точку</blockquote>']

list_advice_llms = [
                    '<blockquote>Режим общения с LLM. Поддерживается только несколько последних моделей из семейства GPT.\n\n',
                    'Если захотите выйти из диалога, отправьте точку</blockquote>']

text_sistem_promt = ('###Answering Rules###\n\n'
                'Follow in the strict order:\n\n'
                '1. USE the language of my message.\n'
                '2. **ONCE PER CHAT** assign a real-world expert role to yourself before answering, e.g., "I will answer as a world-famous historical expert <detailed topic> with <most prestigious LOCAL topic REAL award>" or "I will answer as a world-famous <specific science> expert in the <detailed topic> with <most prestigious LOCAL topic award>" etc.\n'
                '3. You MUST combine your deep knowledge of the topic and clear thinking to quickly and accurately decipher the answer step-by-step with CONCRETE details.\n'
                '4. I am going to tip $1,000,000 for the best reply.\n'
                '5. Your answer is critical for my career.\n'
                '6. Answer the question in a natural, human-like manner.\n'
                '7. ALWAYS use an answering example for a first message structure.\n\n'
                '##Answering in English example##\n\n'
                'I will answer as the world-famous <specific field> scientists with <most prestigious LOCAL award>\n'
                '<Deep knowledge step-by-step answer, with CONCRETE details>'
                )

