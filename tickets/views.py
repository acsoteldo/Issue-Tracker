from collections import OrderedDict

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import DetailView
from django.views.generic.list import ListView
from taggit.models import Tag

from .filters import TicketFilter
from .forms import (
    AcceptTicketForm,
    AssignTicketForm,
    CloseTicketForm,
    CommentTicketForm,
)
from .models import FollowUp, Ticket, UserVoteLog
from .utils import is_admin

User = get_user_model()


class TagMixin(object):
    def get_context_data(self, **kwargs):
        context = super(TagMixin, self).get_context_data(**kwargs)
        tag_slug = self.kwargs.get("slug")
        context["tag"] = Tag.objects.filter(slug=tag_slug).first()
        context["tags"] = Tag.objects.all()
        return context


class TagIndexView(TagMixin, ListView):
    template_name = "tickets/ticket_list.html"
    model = Ticket
    paginate_by = 50  # RECORDS_PER_PAGE

    def get_queryset(self):
        return Ticket.objects.filter(tags__slug=self.kwargs.get("slug"))


class TicketDetailView(DetailView):
    model = Ticket

    def get_context_data(self, **kwargs):
        context = super(TicketDetailView, self).get_context_data(**kwargs)
        user = self.request.user
        pk = context["ticket"].id
        ticket = Ticket.objects.get(id=pk)
        if is_admin(user) or user == ticket.submitted_by:
            comments = FollowUp.all_comments.filter(ticket__pk=pk).order_by(
                "-created_on"
            )
        else:
            comments = FollowUp.objects.filter(ticket__pk=pk).order_by("-created_on")
        context["comments"] = comments

        if ticket and user:
            voter_ids = [x[0] for x in ticket.uservotelog_set.values_list("user_id")]
            has_voted = user.id in voter_ids
        else:
            has_voted = False

        context["has_voted"] = has_voted

        return context


class TicketListViewBase(TagMixin, ListView):

    model = Ticket
    filterset_class = TicketFilter

    def get_context_data(self, **kwargs):
        # Call the base implementation first to get a context
        context = super().get_context_data(**kwargs)

        context["filters"] = get_ticket_filters()
        return context


def get_ticket_filters():
    values = Ticket.TICKET_STATUS_CHOICES
    status = [x[0] for x in values]

    values = sorted(Ticket.TICKET_PRIORITY_CHOICES, key=lambda x: x[0])
    priority = [x[1] for x in values]

    values = Ticket.TICKET_TYPE_CHOICES
    ticket_types = [x[1] for x in values]

    values = Ticket.objects.values_list("application__application").distinct()
    applications = [x[0] for x in values]

    values = Ticket.objects.values_list("submitted_by__username").distinct()
    submitted_by = [x[0] for x in values]

    values = Ticket.objects.values_list("assigned_to__username").distinct()
    assigned_to = [x[0] for x in values]

    ticket_filters = OrderedDict()

    ticket_filters["status"] = status
    ticket_filters["application"] = list(set(applications))
    ticket_filters["priority"] = priority
    ticket_filters["type"] = ticket_types
    ticket_filters["submitted_by"] = list(set(submitted_by))
    ticket_filters["assigned_to"] = list(set(assigned_to))

    return ticket_filters


class TicketListView(TicketListViewBase):

    def get_context_data(self, **kwargs):
        # Call the base implementation first to get a context
        context = super().get_context_data(**kwargs)

        ticket_type = self.kwargs.get("type", None)
        if ticket_type:
            choices = {k: v for k, v in Ticket.TICKET_TYPE_CHOICES}
            context["type"] = choices.get(ticket_type)

        ticket_status = self.kwargs.get("status", None)
        if ticket_status:
            context["status"] = ticket_status.title()

        username = self.kwargs.get("username", None)
        if username:
            context["username"] = username

        context["query"] = self.request.GET.get("q")

        what = self.kwargs.get("what", None)
        if what:
            context["what"] = what.replace("_", " ")

        related_tags = (
            Tag.objects.filter(ticket__id__in=self.object_list)
            .annotate(count=Count("id"))
            .order_by()
        )
        context["related_tags"] = related_tags

        return context

    def get_queryset(self):
        q = self.request.GET.get("q")
        username = self.kwargs.get("username", None)
        what = self.kwargs.get("what", None)
        ticket_type = self.kwargs.get("type", None)
        ticket_status = self.kwargs.get("status", None)

        tickets = Ticket.objects.order_by("-created_on").prefetch_related(
            "application", "submitted_by", "assigned_to"
        )
        if q:
            tickets = tickets.filter(
                Q(description__icontains=q) | Q(title__icontains=q)
            )
        if username:
            if what == "submitted_by":
                tickets = tickets.filter(submitted_by__username=username)
            elif what == "assigned_to":
                tickets = tickets.filter(assigned_to__username=username)
            else:
                tickets = tickets.filter(
                    Q(submitted_by__username=username)
                    | Q(assigned_to__username=username)
                )

        if ticket_type:
            tickets = tickets.filter(ticket_type=ticket_type).order_by("-created_on")

        if ticket_status:
            closed_codes = ["closed", "split", "duplicate"]
            if ticket_status == "closed":
                tickets = Ticket.objects.filter(status__in=closed_codes).order_by(
                    "-created_on"
                )
            else:
                tickets = Ticket.objects.exclude(status__in=closed_codes).order_by(
                    "-created_on"
                )

        # finally - django_filter
        tickets_qs = TicketFilter(self.request.GET, queryset=tickets)

        return tickets_qs.qs


@login_required
def TicketUpdateView(request, pk=None, template_name="tickets/ticket_form.html"):

    if pk:
        ticket = get_object_or_404(Ticket, pk=pk)
        if not (request.user == ticket.submitted_by or is_admin(request.user)):
            return HttpResponseRedirect(ticket.get_absolute_url())
    else:
        ticket = Ticket(submitted_by=request.user, status="new")

    if request.POST:
        form = TicketForm(request.POST, instance=ticket)
        if form.is_valid():
            new_ticket = form.save()
            return HttpResponseRedirect(new_ticket.get_absolute_url())
    else:
        form = TicketForm(instance=ticket)

    return render(request, template_name, {"ticket": ticket, "form": form})


# ==============================
@login_required
def TicketCommentView(request, pk, action="comment"):
    try:
        ticket = Ticket.objects.get(pk=pk)
    except Ticket.DoesNotExist:
        url = reverse("tickets:ticket_list")
        return HttpResponseRedirect(url)

    if not is_admin(request.user) and action != "comment":
        return redirect(ticket.get_absolute_url())

    if action in ("closed", "reopened"):
        template = "tickets/close_reopen_ticket_form.html"
    else:
        template = "tickets/comment_form.html"

    if request.POST:
        if action in ("closed", "reopened"):
            form = CloseTicketForm(
                request.POST, ticket=ticket, user=request.user, action=action
            )
        elif action == "comment":
            form = CommentTicketForm(request.POST, ticket=ticket, user=request.user)
        elif action == "accept":
            form = AcceptTicketForm(request.POST, ticket=ticket, user=request.user)
        else:
            # i.e. action==assign
            form = AssignTicketForm(request.POST, ticket=ticket, user=request.user)

        if form.is_valid():
            form.save()
            return HttpResponseRedirect(ticket.get_absolute_url())
        else:
            render(
                request, template, {"form": form, "ticket": ticket, "action": action}
            )
    else:
        if action in ("closed", "reopened"):
            form = CloseTicketForm(ticket=ticket, user=request.user, action=action)
        elif action == "comment":
            form = CommentTicketForm(ticket=ticket, user=request.user)
        elif action == "accept":
            form = AcceptTicketForm(ticket=ticket, user=request.user)
        else:
            if ticket.assigned_to and action == "assign":
                action = "re-assign"
            form = AssignTicketForm(ticket=ticket, user=request.user)

    return render(request, template, {"form": form, "ticket": ticket, "action": action})
