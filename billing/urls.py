from django.urls import path
from . import views

app_name = 'billing'
urlpatterns = [
    path('customers/', views.customers, name='customers'),
    path('customers/new/', views.customer_edit, name='new_customer'),
    path('customers/<int:pk>/', views.customer_detail, name='customer'),
    path('customers/<int:pk>/edit/', views.customer_edit, name='edit_customer'),
    path('customers/<int:pk>/proposals/<int:proposal_id>/', views.proposal_decide, name='proposal'),
    path('documents/<int:pk>/', views.document_detail, name='document'),
    path('documents/<int:pk>/edit/', views.document_edit, name='edit_document'),
    path('documents/<int:pk>/action/', views.document_action, name='document_action'),
    path('documents/<int:pk>/pdf/', views.document_pdf, name='pdf'),
    path('documents/<int:pk>/pdf/void/', views.void_pdf, name='void_pdf'),
    path('credits/for/<int:credit_for>/new/', views.document_edit, name='new_credit'),
    path('documents/<int:pk>/pdf/<int:copy_id>/', views.document_pdf, name='paid_pdf'),
    path('documents/<int:pk>/send/', views.document_send, name='send'),
    path('documents/<str:kind>/new/', views.document_edit, name='new_document'),
    path('documents/<str:kind>/', views.documents, name='documents'),
    path('payments/', views.payments, name='payments'),
    path('payments/new/', views.payment_new, name='new_payment'),
    path('refunds/<int:pk>/resolve/', views.refund_resolve, name='resolve_refund'),
    path('receipts/<int:pk>/resend/', views.receipt_resend, name='resend_receipt'),
    path('settings/', views.settings_view, name='settings'),
    path('settings/appearance/', views.set_theme, name='set_theme'),
    path('reports/tax/', views.tax_report, name='tax_report'),
    path('recurring/', views.recurring, name='recurring'),
]
