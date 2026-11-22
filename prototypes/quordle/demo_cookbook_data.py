"""Готовит двуязычные учебные рецепты без сети и базы данных."""

from dataclasses import dataclass
from decimal import Decimal
from random import Random


@dataclass(frozen=True)
class DemoRecipe:
    """Содержит исходный рецепт и его готовый английский перевод."""

    recipe_name: str
    description: str
    recipe_name_en: str
    description_en: str
    author: str
    price: Decimal
    image: str


# Вариант содержит два суффикса названия и два завершающих шага (RU/EN).
_Variant = tuple[str, str, str, str]
_Dish = tuple[str, str, str, str, tuple[_Variant, _Variant]]
DEMO_AUTHOR = "Тестовая кухня (синтетический автор)"

_DISHES: tuple[_Dish, ...] = (
    (
        "Тыквенный суп", "Pumpkin soup",
        "Нарежьте 400 г тыквы и луковицу. Варите в 400 мл бульона "
        "20 минут до мягкости, затем измельчите блендером.",
        "Dice 400 g pumpkin and an onion. Simmer in 400 ml stock for "
        "20 minutes until tender, then blend.",
        (
            ("с имбирём", "with ginger",
             "Вмешайте чайную ложку тёртого имбиря и прогрейте минуту.",
             "Stir in a teaspoon of grated ginger and heat for a minute."),
            ("с семечками", "with pumpkin seeds",
             "Подсушите ложку тыквенных семечек и посыпьте каждую порцию.",
             "Toast a spoonful of pumpkin seeds and scatter over each bowl."),
        ),
    ),
    (
        "Гречка с грибами", "Mushroom buckwheat",
        "Сварите 150 г гречки в 300 мл воды. Поджарьте 180 г нарезанных "
        "шампиньонов в ложке масла и перемешайте с крупой.",
        "Cook 150 g buckwheat in 300 ml water. Fry 180 g sliced mushrooms "
        "in a spoonful of oil and stir them into the buckwheat.",
        (
            ("и укропом", "with dill",
             "Снимите с огня и добавьте рубленый укроп.",
             "Take off the heat and fold in chopped dill."),
            ("и луком", "with spring onions",
             "Посыпьте горячую гречку двумя нарезанными перьями лука.",
             "Top the hot buckwheat with two sliced spring onions."),
        ),
    ),
    (
        "Томатная паста", "Tomato pasta",
        "Сварите 160 г пенне. Потушите зубчик чеснока и 200 г "
        "протёртых томатов в ложке масла 8 минут. Перемешайте с пастой.",
        "Cook 160 g penne. Simmer a garlic clove and 200 g passata in a "
        "spoonful of oil for 8 minutes, then toss with the pasta.",
        (
            ("с базиликом", "with basil",
             "Порвите несколько листьев базилика и вмешайте перед подачей.",
             "Tear a few basil leaves and fold them in before serving."),
            ("с оливками", "with olives",
             "Добавьте горсть нарезанных оливок без косточек.",
             "Stir in a handful of sliced pitted olives."),
        ),
    ),
    (
        "Печёный картофель", "Roast potatoes",
        "Нарежьте 450 г картофеля дольками, добавьте ложку масла и соль. "
        "Запекайте при 200 °C 35–40 минут, один раз переверните.",
        "Cut 450 g potatoes into wedges and toss with a spoonful of oil "
        "and salt. Roast at 200 °C for 35–40 minutes, turning once.",
        (
            ("с розмарином", "with rosemary",
             "За 5 минут до готовности добавьте рубленый розмарин.",
             "Add chopped rosemary for the final 5 minutes."),
            ("с чесночным йогуртом", "with garlic yogurt",
             "Смешайте 80 г йогурта с чесноком и подайте отдельно.",
             "Mix 80 g yogurt with crushed garlic and serve alongside."),
        ),
    ),
    (
        "Салат из нута", "Chickpea salad",
        "Промойте 220 г консервированного нута. Нарежьте огурец и помидор "
        "кубиками. Смешайте с нутом и ложкой оливкового масла.",
        "Rinse 220 g canned chickpeas. Dice a cucumber and a tomato and "
        "toss with the chickpeas and a spoonful of olive oil.",
        (
            ("с фетой", "with feta",
             "Раскрошите сверху 50 г феты и аккуратно перемешайте.",
             "Crumble over 50 g feta and toss gently."),
            ("с лимоном и мятой", "with lemon and mint",
             "Добавьте ложку лимонного сока и горсть листьев мяты.",
             "Add a spoonful of lemon juice and a handful of mint leaves."),
        ),
    ),
    (
        "Рис с овощами", "Vegetable rice",
        "Сварите 150 г риса. Морковь и сладкий перец нарежьте мелким "
        "кубиком, жарьте в масле 6 минут и перемешайте с рисом.",
        "Cook 150 g rice. Dice a carrot and a bell pepper, fry in oil for "
        "6 minutes, then mix with the rice.",
        (
            ("и кунжутом", "with sesame",
             "Добавьте ложку обжаренного кунжута.",
             "Stir in a spoonful of toasted sesame seeds."),
            ("и горошком", "with peas",
             "Добавьте 60 г замороженного горошка и прогрейте 4 минуты.",
             "Add 60 g frozen peas and heat for 4 minutes."),
        ),
    ),
    (
        "Овсянка", "Porridge",
        "Залейте 80 г овсяных хлопьев 320 мл молока. Варите 6–8 минут на "
        "слабом огне, помешивая.",
        "Combine 80 g rolled oats and 320 ml milk. Cook over low heat for "
        "6–8 minutes, stirring.",
        (
            ("с яблоком и корицей", "with apple and cinnamon",
             "Вмешайте тёртое яблоко и щепотку корицы.",
             "Stir in a grated apple and a pinch of cinnamon."),
            ("с бананом", "with banana",
             "Разомните половину банана в каше, остаток нарежьте сверху.",
             "Mash half a banana into the oats and slice the rest on top."),
        ),
    ),
    (
        "Чечевичная похлёбка", "Lentil stew",
        "Промойте 140 г красной чечевицы. Добавьте нарезанную морковь, "
        "400 мл воды и соль. Тушите 18–20 минут.",
        "Rinse 140 g red lentils. Add a diced carrot, 400 ml water and "
        "salt. Simmer for 18–20 minutes.",
        (
            ("с паприкой", "with paprika",
             "Вмешайте половину чайной ложки копчёной паприки.",
             "Stir in half a teaspoon of smoked paprika."),
            ("со шпинатом", "with spinach",
             "Добавьте горсть шпината и прогрейте до мягкости.",
             "Add a handful of spinach and cook until wilted."),
        ),
    ),
    (
        "Омлет", "Omelette",
        "Взбейте три яйца с двумя ложками молока. Вылейте на смазанную "
        "маслом сковороду и готовьте под крышкой до схватывания.",
        "Beat three eggs with two spoonfuls of milk. Pour into an oiled "
        "pan, cover and cook until set.",
        (
            ("с сыром", "with cheese",
             "Посыпьте 30 г тёртого сыра, дайте ему расплавиться.",
             "Sprinkle with 30 g grated cheese and let it melt."),
            ("с зеленью", "with herbs",
             "Добавьте рубленые петрушку и укроп, сверните пополам.",
             "Scatter over chopped parsley and dill and fold in half."),
        ),
    ),
    (
        "Кускус", "Couscous",
        "Залейте 140 г кускуса 170 мл горячего бульона. Накройте на "
        "5 минут, затем разрыхлите вилкой.",
        "Pour 170 ml hot stock over 140 g couscous. Cover for 5 minutes, "
        "then fluff with a fork.",
        (
            ("с курагой", "with dried apricots",
             "Нарежьте 40 г кураги и смешайте с кускусом.",
             "Dice 40 g dried apricots and mix into the couscous."),
            ("с миндалём", "with almonds",
             "Посыпьте 30 г поджаренных миндальных лепестков.",
             "Scatter over 30 g toasted flaked almonds."),
        ),
    ),
    (
        "Оладьи из кабачка", "Courgette fritters",
        "Натрите и отожмите 250 г кабачка. Смешайте с яйцом и 40 г "
        "муки. Жарьте небольшие оладьи по 3–4 минуты с каждой стороны.",
        "Grate and squeeze dry 250 g courgette. Mix with an egg and 40 g "
        "flour. Fry small patties for 3–4 minutes per side.",
        (
            ("со сметаной", "with sour cream",
             "К горячим оладьям подайте две ложки сметаны.",
             "Serve the hot fritters with two spoonfuls of sour cream."),
            ("с укропным соусом", "with dill sauce",
             "Смешайте 60 г йогурта с укропом и подайте к оладьям.",
             "Mix 60 g yogurt with dill and serve with the fritters."),
        ),
    ),
    (
        "Творожный крем", "Whipped cottage cheese",
        "Пробейте блендером 200 г творога с 60 г йогурта до гладкости. "
        "Разложите крем в две чашки.",
        "Blend 200 g cottage cheese with 60 g yogurt until smooth. Divide "
        "between two cups.",
        (
            ("с малиной", "with raspberries",
             "Разомните половину горсти малины и выложите сверху с ягодами.",
             "Crush half a handful of raspberries and top with the rest."),
            ("с грушей", "with pear",
             "Нарежьте спелую грушу тонкими ломтиками и добавьте в чашки.",
             "Thinly slice a ripe pear and add it to the cups."),
        ),
    ),
    (
        "Фасолевый паштет", "White bean spread",
        "Промойте 220 г консервированной белой фасоли. Измельчите с "
        "ложкой масла, ложкой воды и щепоткой соли.",
        "Rinse 220 g canned white beans. Blend with a spoonful each of "
        "oil and water and a pinch of salt.",
        (
            ("с лимоном", "with lemon",
             "Добавьте ложку лимонного сока и немного цедры.",
             "Mix in a spoonful of lemon juice and a little grated zest."),
            ("с петрушкой", "with parsley",
             "Мелко нарежьте петрушку, перемешайте и подайте с хлебом.",
             "Finely chop parsley, fold it in and serve with bread."),
        ),
    ),
    (
        "Полента", "Polenta",
        "Всыпьте 80 г поленты быстрого приготовления в 350 мл кипящей "
        "подсоленной воды. Варите 5 минут, постоянно помешивая.",
        "Whisk 80 g instant polenta into 350 ml boiling salted water. "
        "Cook for 5 minutes, stirring constantly.",
        (
            ("с пармезаном", "with Parmesan",
             "Снимите с огня и вмешайте 25 г тёртого пармезана.",
             "Remove from the heat and stir in 25 g grated Parmesan."),
            ("с вялеными томатами", "with sun-dried tomatoes",
             "Нарежьте три вяленых томата и выложите сверху.",
             "Slice three sun-dried tomatoes and scatter over the top."),
        ),
    ),
)


def generate_demo_recipes(seed: int, count: int = 12) -> list[DemoRecipe]:
    """Собирает 10–14 разных блюд; один seed даёт одинаковую книгу."""
    if not 10 <= count <= len(_DISHES):
        raise ValueError("Количество учебных рецептов должно быть от 10 до 14")
    rng = Random(seed)
    dishes = rng.sample(_DISHES, count)
    images = [f"test-fixture:{index % 5 + 1}" for index in range(count)]
    rng.shuffle(images)
    recipes = []
    for (name, name_en, method, method_en, variants), image in zip(
        dishes, images
    ):
        suffix, suffix_en, finish, finish_en = rng.choice(variants)
        recipes.append(DemoRecipe(
            recipe_name=f"{name} {suffix}",
            description=f"{method}\n{finish}",
            recipe_name_en=f"{name_en} {suffix_en}",
            description_en=f"{method_en}\n{finish_en}",
            author=DEMO_AUTHOR,
            price=Decimal(rng.randint(250, 1250)).scaleb(-2),
            image=image,
        ))
    return recipes
