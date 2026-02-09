print("---ПРОСТОЙ КАЛЬКУЛЯТОР---\n")
flag = True

while flag == True:
    num1 = float(input("Введите первое число: "))
    num2 = float(input("Введите второе число: "))

    print("\n---СПИСОК ОПЕРАЦИЙ И ИХ НОМЕР---")
    print("0. Выход из программы.")
    print("1. +")
    print("2. -")
    print("3. *")
    print("4. /")
    a = int(input("Выберите цифру для выполнения операции: "))

    if a == 0:
        print("\nВыход из калькулятора.")
        flag = False
    elif a == 1:
        result = num1 + num2
        print(f"{num1} + {num2} = {result}")
    elif a == 2:
        result = num1 - num2
        print(f"{num1} - {num2} = {result}")
    elif a == 3:
        result = num1 * num2
        print(f"{num1} * {num2} = {result}")
    elif a == 4:
        if num2 == 0:
            print("На ноль делить нельзя!")
        else:
            result = num1 / num2
            print(f"{num1} / {num2} = {result}")
    else:
        print("Выберите номер операции из списка!")