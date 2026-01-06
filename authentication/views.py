# -*- encoding: utf-8 -*-
"""
Authentication views
"""

from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from .forms import LoginForm, SignUpForm
from .models import TelegramUser
from .telegram_utils import get_username_from_telegram, send_message_to_telegram_user, send_message_to_telegram_user_with_image
from .telegram_utils import broadcast_message_to_telegram_users, broadcast_message_to_telegram_users_with_image
import asyncio
from datetime import date, timedelta


def login_view(request):
    form = LoginForm(request.POST or None)
    msg = None

    if request.method == "POST":
        if form.is_valid():
            username = form.cleaned_data.get("username")
            password = form.cleaned_data.get("password")
            user = authenticate(username=username, password=password)
            if user is not None:
                login(request, user)
                return redirect("/")
            else:
                msg = '用户名或密码错误'
        else:
            msg = '表单验证失败'

    return render(request, "accounts/login.html", {"form": form, "msg": msg})


def register_user(request):
    msg = None
    success = False

    if request.method == "POST":
        form = SignUpForm(request.POST)
        if form.is_valid():
            form.save()
            username = form.cleaned_data.get("username")
            msg = '用户创建成功 - 请<a href="/login">登录</a>。'
            success = True
        else:
            msg = '表单验证失败'
    else:
        form = SignUpForm()

    return render(request, "accounts/register.html", {"form": form, "msg": msg, "success": success})


@login_required(login_url="/login/")
def statistics_view(request):
    query = request.GET.get('query')
    if query:
        all_users = TelegramUser.objects.filter(telegram_id__icontains=query)
    else:
        all_users = TelegramUser.objects.all()

    chart_users = TelegramUser.objects.exclude(joining_date=None)

    # Monthly statistics
    month_counts = {month: 0 for month in range(1, 13)}
    for user in chart_users:
        if user.joining_date:
            month = user.joining_date.month
            month_counts[month] += 1

    monthly_data = [month_counts[month] for month in range(1, 13)]

    # Daily statistics for last 7 days
    today = date.today()
    last_seven_days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    last_seven_days_labels = [day.strftime("%Y-%m-%d") for day in last_seven_days]

    daily_data = []
    for i in range(7):
        date_to_check = today - timedelta(days=i)
        users_count = TelegramUser.objects.filter(joining_date=date_to_check).count()
        daily_data.append(users_count)
    daily_data.reverse()

    total_users = all_users.count()
    displayed_users_count = 5

    page = request.GET.get('page')
    if page:
        page = int(page)
        offset = (page - 1) * displayed_users_count
        users = list(all_users.order_by('-telegram_id'))[offset:offset + displayed_users_count]
    else:
        page = 1
        users = list(all_users.order_by('-telegram_id'))[:displayed_users_count]

    # Get usernames from Telegram
    usernames = []
    for user in users:
        try:
            username = asyncio.run(get_username_from_telegram(user.telegram_id))
            usernames.append(username or 'N/A')
        except Exception:
            usernames.append('N/A')

    remaining_users = max(0, total_users - page * displayed_users_count)
    next_page = page + 1 if remaining_users > 0 else None

    context = {
        'total': total_users,
        'user_count': len(users),
        'users': zip(users, usernames),
        'remaining_users': remaining_users,
        'next_page': next_page,
        'monthly_data': monthly_data,
        'daily_data': daily_data,
        'last_seven_days': last_seven_days_labels,
        'query': query
    }
    return render(request, 'index.html', context)


@login_required(login_url="/login/")
def send_message_api(request):
    if request.method == 'POST':
        user_id = request.POST.get('user_id')
        message = request.POST.get('message')
        image = request.FILES.get('image')

        if image:
            image_data = image.read()
            asyncio.run(send_message_to_telegram_user_with_image(user_id, message, image_data))
        else:
            asyncio.run(send_message_to_telegram_user(user_id, message))

    return render(request, 'index.html')


@login_required(login_url="/login/")
def broadcast_message_api(request):
    if request.method == 'POST':
        message = request.POST.get('message')
        all_users = TelegramUser.objects.all()
        chat_ids = [user.telegram_id for user in all_users]
        image = request.FILES.get('image')

        if image:
            image_data = image.read()
            asyncio.run(broadcast_message_to_telegram_users_with_image(message, chat_ids, image_data))
        else:
            asyncio.run(broadcast_message_to_telegram_users(message, chat_ids))

    return render(request, 'index.html')
