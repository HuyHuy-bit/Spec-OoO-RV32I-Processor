`default_nettype none
module data_port_arbiter (
  input wire clk_i, rst_i,
  input wire [2:0] reserve_i, client_fatal_i,
  output wire [2:0] grant_o, owner_o,
  input wire [2:0] client_request_valid_i,
  output logic [2:0] client_request_ready_o,
  input memory_protocol_pkg::mem_request_t [2:0] client_request_i,
  output logic [2:0] client_response_valid_o,
  input wire [2:0] client_response_ready_i,
  output memory_protocol_pkg::mem_response_t client_response_o,
  output logic request_valid_o,
  input wire request_ready_i,
  output memory_protocol_pkg::mem_request_t request_o,
  input wire response_valid_i,
  output logic response_ready_o,
  input memory_protocol_pkg::mem_response_t response_i,
  output wire busy_o,
  output logic fatal_o,
  output logic [1:0] fatal_reason_o,
  output logic [2:0] fatal_clients_o
);
  typedef enum logic [1:0] {IDLE, REQUEST, RESPONSE} state_e;
  state_e state_q;
  logic [1:0] owner_q, next_q, selected;
  logic [2:0] selection;
  wire active = !rst_i && !fatal_o && client_fatal_i == 0;
  wire unexpected = response_valid_i && state_q != RESPONSE;
  assign busy_o = !rst_i && state_q != IDLE;
  assign owner_o = busy_o ? (3'b001 << owner_q) : 3'b000;
  assign grant_o = active && !unexpected && state_q == IDLE ? selection : 3'b000;
  assign client_response_o = response_i;

  always_comb begin
    selection = 0;
    selected = 0;
    for (int offset = 2; offset >= 0; offset--) begin
      logic [1:0] index;
      index = 2'((int'(next_q) + offset) % 3);
      if (reserve_i[index]) begin
        selection = 0;
        selection[index] = 1;
        selected = 2'(index);
      end
    end
    request_valid_o = 0;
    request_o = '0;
    client_request_ready_o = 0;
    client_response_valid_o = 0;
    response_ready_o = 0;
    if (active && state_q == REQUEST) begin
      request_valid_o = client_request_valid_i[owner_q];
      if (request_valid_o) request_o = client_request_i[owner_q];
      client_request_ready_o[owner_q] = request_ready_i;
    end
    if (active && state_q == RESPONSE) begin
      client_response_valid_o[owner_q] = response_valid_i;
      response_ready_o = client_response_ready_i[owner_q];
    end
  end

  // Registered client faults block the next grant without a response-completion bubble.
  always_ff @(posedge clk_i) begin
    if (rst_i) begin
      state_q <= IDLE;
      next_q <= 0;
      fatal_o <= 0;
      fatal_reason_o <= 0;
      fatal_clients_o <= 0;
    end else if (!fatal_o) begin
      if (client_fatal_i != 0 || unexpected) begin
        fatal_o <= 1;
        fatal_reason_o <= client_fatal_i != 0 ? 2'd2 : 2'd1;
        fatal_clients_o <= client_fatal_i;
      end else case (state_q)
        IDLE: if (grant_o != 0) begin
          owner_q <= selected;
          next_q <= selected == 2 ? 2'd0 : selected + 1'b1;
          state_q <= REQUEST;
        end
        REQUEST: if (request_valid_o && request_ready_i) state_q <= RESPONSE;
        RESPONSE: if (response_valid_i && response_ready_o) state_q <= IDLE;
        default: ;
      endcase
    end
  end
`ifndef SYNTHESIS
  always_ff @(posedge clk_i) if (active && !unexpected) begin
    assert (state_q != REQUEST || client_request_valid_i[owner_q])
      else $fatal(1, "DATA_PORT_GRANTED_OFFER");
    assert (!client_request_valid_i[0] || owner_o[0]) else $fatal(1, "DATA_PORT_LOAD_OWNER");
    assert (!client_request_valid_i[2] || owner_o[2]) else $fatal(1, "DATA_PORT_MMIO_OWNER");
  end
`endif
endmodule
`default_nettype wire
