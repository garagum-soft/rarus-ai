// Shared by the account gateway's pages: words in the reader's language, and the JSON API.
(function () {
  "use strict";

  var WORDS = {
    en: {
      title: "Raven",
      signin: "Sign in",
      register: "Create an account",
      username: "Username",
      password: "Password",
      newPassword: "New password",
      currentPassword: "Current password",
      email: "E-mail (optional)",
      google: "Sign in with Google",
      or: "or",
      setupTitle: "Set up this server",
      setupText: "No account exists yet. The account you create now is the superadmin: it can do everything, and its Raven is the one already on this server.",
      setupRemote: "This server is not set up yet. Its first account is made on the server itself: open http://localhost on it, or run `raven accounts init` in its terminal.",
      create: "Create",
      registrationClosed: "Registration is closed; ask an administrator for an account.",
      starting: "Starting your Raven...",
      account: "Account",
      admin: "Admin",
      signout: "Sign out",
      back: "Back to Raven",
      credits: "credits",
      balance: "Balance",
      disk: "Disk",
      blocked: "New work is paused",
      history: "History",
      when: "When",
      what: "What",
      change: "Change",
      save: "Save",
      linkGoogle: "Link Google",
      googleLinked: "Google is linked",
      saved: "Saved",
      accounts: "Accounts",
      prices: "Prices",
      settings: "Settings",
      audit: "Audit",
      ledger: "Ledger",
      role: "Role",
      engine: "Engine",
      lent: "Lent access",
      topup: "Top up",
      disable: "Disable",
      enable: "Enable",
      restart: "Restart",
      permissions: "Permissions",
      model: "Model",
      input: "Input",
      output: "Output",
      cacheHit: "Cache hit",
      remove: "Remove",
      add: "Add",
      perMillion: "US dollars per million tokens",
      pricesNote: "* prices every model with no row of its own; cli:* prices the CLI agents, whose usage is estimated from their text.",
      creditsPerUsd: "Credits per US dollar",
      ownShare: "Share charged on the account's own keys and subscriptions",
      lentShare: "Share charged on access the server lends",
      unpriced: "Models used with no price of their own (charged at *):",
      publicUrl: "Public address (https://...)",
      registrationOpen: "Anyone can create an account",
      googleId: "Google OAuth client ID",
      googleSecret: "Google OAuth client secret (leave empty to keep the stored one)",
      googleRedirect: "Authorized redirect URI to add in the Google Cloud console:",
      diskQuota: "Disk allowance per account, GB",
      newCredits: "Welcome bonus: credits every new account starts with",
      newAccount: "New account",
      securityNote: "All engines run as the same operating-system user as this server. Accounts are kept apart by Raven, not by the operating system: an agent allowed to run shell commands without a sandbox can read other accounts' files. Turn on a sandbox (tools.sandbox.backend) in each engine, or give people only accounts you trust.",
      estimated: "estimated"
    },
    ru: {
      title: "Raven",
      signin: "Войти",
      register: "Создать аккаунт",
      username: "Имя пользователя",
      password: "Пароль",
      newPassword: "Новый пароль",
      currentPassword: "Текущий пароль",
      email: "E-mail (необязательно)",
      google: "Войти через Google",
      or: "или",
      setupTitle: "Настройка сервера",
      setupText: "Аккаунтов ещё нет. Аккаунт, который вы создадите сейчас, станет суперадмином: ему доступно всё, а его Raven — тот, что уже работает на этом сервере.",
      setupRemote: "Сервер ещё не настроен. Первый аккаунт создаётся на самом сервере: откройте на нём http://localhost или выполните `raven accounts init` в его терминале.",
      create: "Создать",
      registrationClosed: "Регистрация закрыта; попросите аккаунт у администратора.",
      starting: "Запускаем ваш Raven...",
      account: "Аккаунт",
      admin: "Админка",
      signout: "Выйти",
      back: "Вернуться в Raven",
      credits: "кредитов",
      balance: "Баланс",
      disk: "Диск",
      blocked: "Новая работа приостановлена",
      history: "История",
      when: "Когда",
      what: "Что",
      change: "Изменить",
      save: "Сохранить",
      linkGoogle: "Привязать Google",
      googleLinked: "Google привязан",
      saved: "Сохранено",
      accounts: "Аккаунты",
      prices: "Цены",
      settings: "Настройки",
      audit: "Журнал",
      ledger: "Списания",
      role: "Роль",
      engine: "Движок",
      lent: "Выданный доступ",
      topup: "Пополнить",
      disable: "Отключить",
      enable: "Включить",
      restart: "Перезапустить",
      permissions: "Права",
      model: "Модель",
      input: "Вход",
      output: "Выход",
      cacheHit: "Кэш (hit)",
      remove: "Удалить",
      add: "Добавить",
      perMillion: "долларов США за миллион токенов",
      pricesNote: "* — цена для всех моделей без своей строки; cli:* — для CLI-агентов, расход которых оценивается по тексту.",
      creditsPerUsd: "Кредитов за 1 доллар",
      ownShare: "Доля цены на своих ключах и подписках аккаунта",
      lentShare: "Доля цены на доступе, выданном сервером",
      unpriced: "Модели без своей цены (списывается по *):",
      publicUrl: "Публичный адрес (https://...)",
      registrationOpen: "Любой может создать аккаунт",
      googleId: "Google OAuth client ID",
      googleSecret: "Google OAuth client secret (оставьте пустым, чтобы не менять)",
      googleRedirect: "Authorized redirect URI для Google Cloud console:",
      diskQuota: "Диск на аккаунт, ГБ",
      newCredits: "Приветственный бонус: кредитов каждому новому аккаунту",
      newAccount: "Новый аккаунт",
      securityNote: "Все движки работают от того же пользователя ОС, что и сервер. Аккаунты разделяет Raven, а не операционная система: агент, которому разрешены команды оболочки без песочницы, может прочитать файлы других аккаунтов. Включите песочницу (tools.sandbox.backend) в каждом движке или выдавайте аккаунты только тем, кому доверяете.",
      estimated: "оценка"
    }
  };

  var lang = (navigator.language || "en").toLowerCase().indexOf("ru") === 0 ? "ru" : "en";

  function t(key) {
    return (WORDS[lang] && WORDS[lang][key]) || WORDS.en[key] || key;
  }

  function translate(root) {
    (root || document).querySelectorAll("[data-i18n]").forEach(function (el) {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    (root || document).querySelectorAll("[data-i18n-placeholder]").forEach(function (el) {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")));
    });
  }

  function api(path, body) {
    var options = body === undefined
      ? { credentials: "same-origin" }
      : { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    return fetch(path, options).then(function (response) {
      return response.text().then(function (text) {
        var data;
        try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { error: text }; }
        if (!response.ok) {
          var error = new Error((data && data.error) || text || ("HTTP " + response.status));
          error.status = response.status;
          throw error;
        }
        return data;
      });
    });
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function credits(n) {
    return (Math.round(Number(n || 0) * 100) / 100).toLocaleString(lang);
  }

  function when(ts) {
    return new Date(Number(ts) * 1000).toLocaleString(lang);
  }

  window.RavenHub = { t: t, translate: translate, api: api, esc: esc, credits: credits, when: when, lang: lang };
  document.addEventListener("DOMContentLoaded", function () { translate(); });
})();
